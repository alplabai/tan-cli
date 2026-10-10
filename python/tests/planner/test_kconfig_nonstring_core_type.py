# SPDX-License-Identifier: Apache-2.0
"""`_emit_inference`'s CPU-class TFLM kernel selector (tan-cli#962): the same
unguarded `(c.get("type") or "").lower()` `tan.planner.topology.core_os_topology`
and `presets_cmd.core_type_lookup` carried for tan-cli#957, still live at
`kconfig.py:1174` after that sweep -- in the `tan build` path this time, not
`tan presets`.

A read-only sweep found a fifth live instance two lines above the one #962
fixed: the same selector's `vec = (c.get("vector_extension") or "").lower()`
carried the identical bare pre-#957 idiom. This is now guarded the same way,
and covered here alongside `type` (independently parametrized, each held at
a valid control value while the other varies) -- there is no schema
validator anywhere in tan-cli's read path for `soc_spec` (see
`changelog.d/957.fixed.md`), so nothing stops either field from arriving as
any of these non-string shapes and this file only proves the two sites this
sweep actually found, not that no sixth site remains.

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


def _project(type_value, vector_extension=None):
    from tan.planner.models import BoardProject, Slice

    # `inference={"default_arena_kib": 64}` makes `_slice_wants_inference`
    # return True on the `slice_.inference` signal alone, so the call
    # reaches the G-2 kernel-selector loop without needing a resolved
    # `libraries:` alias table.
    #
    # `vector_extension` is always keyed into the core dict (even when the
    # caller passes the default `None`) -- `dict.get` cannot tell "key
    # absent" from "key present with value None" apart, so this keeps the
    # `type`-only callers and the `vector_extension`-only callers on the
    # identical shape and exercises the isinstance guard's `None` branch on
    # a key that genuinely exists in the soc_spec.
    return BoardProject(
        sku="E1M-TEST",
        hw_rev=None,
        board_name=None,
        board_hw_rev=None,
        cores={"a55": Slice(core_id="a55", os="yocto",
                             inference={"default_arena_kib": 64})},
        ipc=[],
        soc_spec={"cores": [{
            "id": "a55",
            "type": type_value,
            "vector_extension": vector_extension,
        }]},
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


@pytest.mark.parametrize(
    "vector_extension",
    [7, ["neon"], {"a": 1}, True, None, 0, []],
    ids=["int", "list", "dict", "bool", "null", "zero", "emptylist"],
)
def test_emit_inference_normalises_a_nonstring_vector_extension_never_raises(
    planner, vector_extension
):
    """The fifth live instance of the #957 family (found two lines above the
    `type` site #962 fixed, in the same G-2 loop): `vec = (c.get(
    "vector_extension") or "").lower()` carried the identical bare
    pre-#957 idiom. `type` is held at a valid, non-`cortex-a*` string
    (`"cortex-m33"`) throughout so this test exercises `vector_extension`
    independently of the `type` guard proven above -- the REF fallback here
    can only come from `vec`, never from `ctype.startswith("cortex-a")`.

    Mutation-proven: reverting `raw_vec = c.get("vector_extension"); vec =
    raw_vec.lower() if isinstance(raw_vec, str) else ""` back to the bare
    `vec = (c.get("vector_extension") or "").lower()` it replaced turns the
    four TRUTHY-non-string cases here (`int`, `list`, `dict`, `bool`) RED on
    a real `AttributeError: '<type>' object has no attribute 'lower'` -- the
    three falsy shapes (`null`, `zero`, `emptylist`) stay green either way.
    Restoring turns all seven GREEN again.
    """
    from tan.planner.kconfig import _emit_inference

    project = _project("cortex-m33", vector_extension=vector_extension)
    slice_ = project.cores["a55"]

    lines = _emit_inference(project, slice_, silicon=None)

    kernel_lines = [ln for ln in lines if "TFLM_KERNEL" in ln]
    assert kernel_lines == ["CONFIG_ALP_SDK_INFERENCE_TFLM_KERNEL_REF=y"], (
        f"a non-string vector_extension must fall back to the REF kernel, "
        f"not crash or silently pick NEON/HELIUM: got {kernel_lines!r}")


def test_emit_inference_still_picks_neon_for_a_real_neon_string(planner):
    """Control: the guard must not turn a genuinely valid `"neon"` string
    into the REF fallback -- only a non-string `vector_extension` degrades.
    `type` is held at a non-`cortex-a*` string so only `vec` drives NEON
    here."""
    from tan.planner.kconfig import _emit_inference

    project = _project("cortex-m33", vector_extension="neon")
    slice_ = project.cores["a55"]

    lines = _emit_inference(project, slice_, silicon=None)

    kernel_lines = [ln for ln in lines if "TFLM_KERNEL" in ln]
    assert kernel_lines == ["CONFIG_ALP_SDK_INFERENCE_TFLM_KERNEL_NEON=y"]
