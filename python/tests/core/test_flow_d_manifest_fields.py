# SPDX-License-Identifier: Apache-2.0
"""tan-cli#353 (reopened): tan's OWN in-process planner never resolved
`flash_args.expect_dpidr` / `jlink_device` / `slot0_load_address` for an AEN
Flow D slice -- it correctly emitted `jlink_flash_device` (the part-number
flash-algorithm profile) and then silently dropped the three facts that arm
the wrong-board preflight and tell Flow D where the slot0-linked app blob
belongs. alp-sdk's OWN emitter (`scripts/alp_project.py --emit
system-manifest`, once alp-sdk PR #1374 lands) produces all four keys; tan's
relocated planner (`tan/planner/loader.py` + `tan/planner/orchestrator.py`)
did not, because it was ported from an alp-sdk commit that predates both
alp-sdk #1362 (expect_dpidr/jlink_device) and #1374 (slot0_load_address) --
so a project built end-to-end through `tan alone` (which plans in-process,
never shelling `alp_project.py`) never carried them regardless of what
alp-sdk's own script does.

Drives `tan.planner.loader` + `tan.planner.orchestrator` directly, on
SYNTHETIC `soc_spec` / `som_preset` dicts -- not a real alp-sdk checkout, so
this suite needs no `ALP_SDK_ROOT` and cannot skip. `tan.planner`'s package
`__init__` reads real `metadata/registries/*` files at import time (see
`tests/core/test_sdk_revision_gate.py`'s own docstring), so the two modules
under test are loaded standalone, under a synthetic parent package rooted at
the real `tan/planner/` directory, bypassing that `__init__` -- the same
technique `test_sdk_revision_gate.py::_load_sdk_compat_standalone` uses for
`sdk_compat.py`. Neither `loader.py` nor `orchestrator.py` (nor any module
they import at module scope: `models`, `partition`, `paths`, `som_metadata`,
`strict_loaders`, `topology`, `validate`, `sdk_compat`, `secure`,
`zephyr_board`, `project_loader`) reads a file at IMPORT time -- every
`Path.read_text()` in that closure is inside a function body -- so this needs
only a syntactically-valid (needn't exist on disk with real metadata) bound
`sdk_root` for `tan.planner_root.sdk_root()` to resolve, never touched by any
call this suite makes.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_STANDALONE_PKG = "tan_planner_standalone_353"


def _bind_stub_sdk_root() -> None:
    """Ensure `tan.planner_root.sdk_root()` resolves to SOMETHING, without
    requiring a real alp-sdk checkout: `paths.py` (imported transitively by
    both modules under test) evaluates `REPO = sdk_root()` at module scope
    and only performs a `Path` join on it -- no file is read off `REPO` by
    anything this suite calls. If a real `ALP_SDK_ROOT` was already bound by
    an earlier test in this process (e.g. `test_sdk_revision_gate.py`'s
    `planner` fixture), reuse it rather than fighting `bind_sdk_root`'s
    reject-a-rebind guard -- either way the bound value is inert here.
    """
    from tan.planner_root import PlannerRootError, bind_sdk_root, sdk_root

    try:
        sdk_root()
    except PlannerRootError:
        bind_sdk_root(Path(__file__).resolve().parent)


def _load_planner_module(module_name: str) -> types.ModuleType:
    """Load `tan/planner/<module_name>.py` under a synthetic parent package
    (`_STANDALONE_PKG`) rooted at the REAL `tan/planner/` directory, so its
    `from .sibling import x` relative imports resolve to the real sibling
    files without ever importing `tan.planner`'s own `__init__.py`.
    """
    import tan

    planner_dir = Path(tan.__file__).parent / "planner"
    if _STANDALONE_PKG not in sys.modules:
        pkg = types.ModuleType(_STANDALONE_PKG)
        pkg.__path__ = [str(planner_dir)]
        sys.modules[_STANDALONE_PKG] = pkg

    full_name = f"{_STANDALONE_PKG}.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(
        full_name, planner_dir / f"{module_name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def loader_mod():
    _bind_stub_sdk_root()
    return _load_planner_module("loader")


@pytest.fixture(scope="module")
def orchestrator_mod():
    _bind_stub_sdk_root()
    return _load_planner_module("orchestrator")


# --------------------------------------------------------------------------
# Fixtures shaped like E1M-AEN801 / the AE822 SoC variant (real values,
# bench-measured per tan-cli#353's own issue comments: expect_dpidr
# 0x4C013477, jlink_device Cortex-M55, hp_slot0 0x802b0000, he_slot0
# 0x80010000).
# --------------------------------------------------------------------------


def _disjoint_soc_spec() -> dict:
    return {
        "variants": [
            {
                "order_code": "AE822FA0E5597LS0",
                "alp_module_skus": ["E1M-AEN801"],
                "debug": {
                    "jlink_flash_device": "AE822FA0E5597LS0_M55_HE",
                    "expect_dpidr": "0x4C013477",
                    "jlink_device": {
                        "m55_hp": "Cortex-M55",
                        "m55_he": "Cortex-M55",
                    },
                },
            }
        ]
    }


def _disjoint_som_preset() -> dict:
    return {
        "sku": "E1M-AEN801",
        "silicon_variant": "AE822FA0E5597LS0",
        "memory_map": [
            {"name": "mcuboot", "base": 0x80000000, "size_kib": 64,
             "accessible_from": None},
            {"name": "hp_slot0", "base": 0x802B0000, "size_kib": 2688,
             "accessible_from": ["m55_hp"]},
            {"name": "he_slot0", "base": 0x80010000, "size_kib": 2688,
             "accessible_from": ["m55_he"]},
        ],
    }


def _flash_args_for(loader_mod, orchestrator_mod, soc_spec, som_preset,
                     core_id: str, os_: str = "zephyr"):
    debug = loader_mod._resolve_variant_debug(som_preset, soc_spec)
    jlink_flash_device = loader_mod._resolve_jlink_flash_device(debug)
    expect_dpidr, jlink_device = loader_mod._resolve_flow_d_preflight(
        debug, core_id)
    slot0_load_address = (
        loader_mod._resolve_slot0_load_address(som_preset, core_id)
        if jlink_flash_device else None)
    slice_ = loader_mod._slice_from_resolved(
        core_id, {"os": os_},
        jlink_flash_device=jlink_flash_device,
        expect_dpidr=expect_dpidr,
        jlink_device=jlink_device,
        slot0_load_address=slot0_load_address,
    )
    return orchestrator_mod._slice_flash_recipe(slice_)


# --------------------------------------------------------------------------
# The regression itself: all four keys must survive into flash_args.
# --------------------------------------------------------------------------


def test_flow_d_flash_args_carries_all_four_keys_for_m55_hp(
        loader_mod, orchestrator_mod):
    method, args = _flash_args_for(
        loader_mod, orchestrator_mod,
        _disjoint_soc_spec(), _disjoint_som_preset(), "m55_hp")
    assert method == "zephyr_west_flash"
    assert args == {
        "jlink_flash_device": "AE822FA0E5597LS0_M55_HE",
        "expect_dpidr": "0x4C013477",
        "jlink_device": "Cortex-M55",
        "slot0_load_address": "0x802b0000",
    }


def test_flow_d_flash_args_carries_all_four_keys_for_m55_he(
        loader_mod, orchestrator_mod):
    method, args = _flash_args_for(
        loader_mod, orchestrator_mod,
        _disjoint_soc_spec(), _disjoint_som_preset(), "m55_he")
    assert method == "zephyr_west_flash"
    assert args == {
        "jlink_flash_device": "AE822FA0E5597LS0_M55_HE",
        "expect_dpidr": "0x4C013477",
        "jlink_device": "Cortex-M55",
        "slot0_load_address": "0x80010000",
    }


def test_a_core_with_no_jlink_flash_device_arms_no_flow_d_keys(
        loader_mod, orchestrator_mod):
    """A non-AEN / non-Flow-D core (e.g. a32_cluster, or any SoC variant
    that publishes no `jlink_flash_device`) must stay exactly as before --
    no shape change for the slices this fix does not touch."""
    soc_spec = {"variants": [{"order_code": "X", "alp_module_skus": [],
                               "debug": {}}]}
    som_preset = {"sku": "E1M-AEN801", "silicon_variant": "X"}
    method, args = _flash_args_for(
        loader_mod, orchestrator_mod, soc_spec, som_preset, "m55_hp")
    assert method == "zephyr_west_flash"
    assert args == {}


def test_stock_no_override_falls_back_to_the_documented_address(
        loader_mod, orchestrator_mod):
    """A single-M55 AEN SKU (or an AEN801-shaped preset with no
    `memory_map:` override) has no per-role `<role>_slot0` entry at all --
    `slot0_load_address` must fall back to the bench-proven stock address
    0x80010000 (docs/aen-bench-bringup.md, docs/secure-boot.md), not go
    missing."""
    soc_spec = _disjoint_soc_spec()
    som_preset = {"sku": "E1M-AEN601", "silicon_variant": "AE822FA0E5597LS0"}
    method, args = _flash_args_for(
        loader_mod, orchestrator_mod, soc_spec, som_preset, "m55_he")
    assert args["slot0_load_address"] == "0x80010000"


# --------------------------------------------------------------------------
# Both are coded refusals, never a silent drop or a half-armed write.
# --------------------------------------------------------------------------


def test_a_half_armed_preflight_pair_is_a_coded_refusal(loader_mod):
    """`debug.expect_dpidr` present but `debug.jlink_device` missing this
    core's entry is a real metadata gap, not "unarmed" -- must raise, naming
    the core id and the remedy, never silently degrade to an unguarded
    write."""
    soc_spec = {
        "variants": [{
            "order_code": "X",
            "alp_module_skus": [],
            "debug": {
                "jlink_flash_device": "X_DEVICE",
                "expect_dpidr": "0xDEADBEEF",
                "jlink_device": {"m55_hp": "Cortex-M55"},  # m55_he missing
            },
        }]
    }
    som_preset = {"sku": "E1M-AEN801", "silicon_variant": "X"}
    debug = loader_mod._resolve_variant_debug(som_preset, soc_spec)
    jlink_flash_device = loader_mod._resolve_jlink_flash_device(debug)
    expect_dpidr, jlink_device = loader_mod._resolve_flow_d_preflight(
        debug, "m55_he")
    slice_ = loader_mod._slice_from_resolved(
        "m55_he", {"os": "zephyr"},
        jlink_flash_device=jlink_flash_device,
        expect_dpidr=expect_dpidr,
        jlink_device=jlink_device,
    )
    with pytest.raises(loader_mod.OrchestratorError) as excinfo:
        loader_mod._enforce_flow_d_preflight_pair(slice_, debug, "E1M-AEN801")
    message = str(excinfo.value)
    assert "m55_he" in message
    assert "debug.jlink_device" in message


def test_a_half_authored_memory_map_is_a_coded_refusal_not_a_guess(
        loader_mod):
    """A `memory_map:` that declares HP's disjoint slot0 but not HE's is a
    half-authored map -- silently falling back to the stock address for HE
    would land its slot0 on top of HP's declared window (alp-sdk #1069's
    bench-confirmed corruption). Must raise naming the core, never guess."""
    som_preset = {
        "sku": "E1M-AEN801",
        "memory_map": [
            {"name": "mcuboot", "base": 0x80000000, "size_kib": 64,
             "accessible_from": None},
            {"name": "hp_slot0", "base": 0x802B0000, "size_kib": 2688,
             "accessible_from": ["m55_hp"]},
            # he_slot0 deliberately absent
        ],
    }
    with pytest.raises(loader_mod.OrchestratorError) as excinfo:
        loader_mod._resolve_slot0_load_address(som_preset, "m55_he")
    assert "m55_he" in str(excinfo.value)
