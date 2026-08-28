# SPDX-License-Identifier: Apache-2.0
"""`_emit_inference`'s CPU-class TFLM kernel selector (tan-cli#962): the same
unguarded `(c.get("type") or "").lower()` `tan.planner.topology.core_os_topology`
and `presets_cmd.core_type_lookup` carried for tan-cli#957, still live at
`kconfig.py:1174` after that sweep -- in the `tan build` path this time, not
`tan presets`.

Same requirement as `test_topology_nonstring_core_type.py` (the sibling this
file's fixture is copied from): `tan.planner.kconfig` cannot be imported
before `bind_sdk_root(<checkout>)` has run, so this needs a real
`ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT` and skips, loudly, without one -- never a
silent pass.
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
    "root and import (same requirement as test_topology_nonstring_core_type.py)"
)


@pytest.fixture(scope="module")
def planner():
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    import tan.planner as planner_pkg

    return planner_pkg


def _project(type_value):
    from tan.planner.models import BoardProject, Slice

    # `inference={"default_arena_kib": 64}` makes `_slice_wants_inference`
    # return True on the `slice_.inference` signal alone, so the call
    # reaches the G-2 kernel-selector loop without needing a resolved
    # `libraries:` alias table.
    return BoardProject(
        sku="E1M-TEST",
        hw_rev=None,
        board_name=None,
        board_hw_rev=None,
        cores={"a55": Slice(core_id="a55", os="yocto",
                             inference={"default_arena_kib": 64})},
        ipc=[],
        soc_spec={"cores": [{"id": "a55", "type": type_value}]},
        som_preset={},
        board_preset=None,
    )


@pytest.mark.parametrize(
    "type_value",
    [7, ["cortex-a55"], {"a": 1}, True, None, 0, []],
    ids=["int", "list", "dict", "bool", "null", "zero", "emptylist"],
)
def test_emit_inference_normalises_a_nonstring_core_type_never_raises(
    planner, type_value
):
    """A schema-invalid `soc_spec` (the shape `soc-spec-v1.schema.json`'s own
    `"type": {"type": "string"}` forbids, but nothing stops a hand-authored,
    mid-`porting-a-new-som`, or corrupted SoC JSON from producing) must not
    raise `AttributeError` out of the G-2 kernel-selector's
    `(c.get("type") or "").lower()`, and must fall back to the REF kernel
    (the same answer a genuinely absent/unresolved `type` already produces)
    rather than crash the whole `tan build`.

    Mutation-proven: reverting `raw_type = c.get("type"); ctype =
    raw_type.lower() if isinstance(raw_type, str) else ""` back to the bare
    `ctype = (c.get("type") or "").lower()` it replaced turns the four
    TRUTHY-non-string cases here (`int`, `list`, `dict`, `bool`) RED on a
    real `AttributeError: '<type>' object has no attribute 'lower'` -- the
    bare `or ""` already tolerated the three falsy shapes (`null`, `zero`,
    `emptylist`), so those three stay green either way; the guard's job is
    exactly the truthy half. Restoring turns all seven GREEN again.
    """
    from tan.planner.kconfig import _emit_inference

    project = _project(type_value)
    slice_ = project.cores["a55"]

    lines = _emit_inference(project, slice_, silicon=None)

    kernel_lines = [ln for ln in lines if "TFLM_KERNEL" in ln]
    assert kernel_lines == ["CONFIG_ALP_SDK_INFERENCE_TFLM_KERNEL_REF=y"], (
        f"a non-string core type must fall back to the REF kernel, not "
        f"crash or silently pick NEON/HELIUM: got {kernel_lines!r}")


def test_emit_inference_still_picks_neon_for_a_real_cortex_a_string(planner):
    """Control: the guard must not turn a genuinely valid `cortex-a*` string
    into the REF fallback -- only a non-string degrades."""
    from tan.planner.kconfig import _emit_inference

    project = _project("cortex-a55")
    slice_ = project.cores["a55"]

    lines = _emit_inference(project, slice_, silicon=None)

    kernel_lines = [ln for ln in lines if "TFLM_KERNEL" in ln]
    assert kernel_lines == ["CONFIG_ALP_SDK_INFERENCE_TFLM_KERNEL_NEON=y"]
