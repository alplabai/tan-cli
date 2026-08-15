# SPDX-License-Identifier: Apache-2.0
"""som.sku -> SoM preset -> SoC npus[] -> compile targets.

`resolve_targets` pulls `resolve_soc_path` from the leaf module `tan.soc_ref`
(ADR-0028 Task 2 follow-up decoupling: `tan.model` no longer imports
`tan.planner`, see `test_model_package_imports.py`) -- so
`from tan.model.targets import resolve_targets` imports fine with no SDK
bound at all. What these tests need a bound, real alp-sdk checkout for is the
DATA, not the import: `_META` must resolve real, committed SoM presets
(`E1M-AEN701.yaml`, `E1M-V2N101.yaml`, ...) and SoC JSON under
`metadata/socs/` -- a throwaway fixture tree has none of that content. Bind +
resolve happen together, at module scope, guarded by the same `SDK is None`
check `pytestmark` skips on."""
import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- resolve_targets imports fine standalone, but these "
           "tests assert against real committed SoM presets and SoC JSON "
           "that only exist in a bound metadata/ tree.",
)

if SDK is not None:
    bind_sdk_root(SDK)
    from tan.model.targets import resolve_targets, TargetSpec
    _META = SDK / "metadata"
else:
    resolve_targets = TargetSpec = None  # unreachable: every test here is skipped
    _META = None


def test_resolve_targets_for_aen701_yields_ethos_u_accel_configs_plus_cpu():
    specs = resolve_targets("E1M-AEN701", metadata_root=_META)
    by = {(s.backend, s.accel_config) for s in specs}
    assert ("ethos_u", "ethos-u55-256") in by
    assert ("ethos_u", "ethos-u55-128") in by
    assert ("cpu", "") in by
    eu = next(s for s in specs if s.backend == "ethos_u")
    assert eu.silicon_ref == "alif:ensemble:e7"
    assert next(s for s in specs if s.backend == "cpu").silicon_ref == "*"


def test_resolve_targets_dedupes_identical_accel_configs():
    specs = resolve_targets("E1M-AEN701", metadata_root=_META)
    ethos = [s for s in specs if s.backend == "ethos_u"]
    assert len(ethos) == 2                                     # one per distinct accel_config


def test_resolve_targets_for_v2n101_yields_drpai_plus_cpu():
    # E1M-V2N101 -> renesas:rzv2n:n44 -> DRP-AI NPU + cpu
    specs = resolve_targets("E1M-V2N101", metadata_root=_META)
    backends = {s.backend for s in specs}
    assert "drpai" in backends
    assert "cpu" in backends
    drp = next(s for s in specs if s.backend == "drpai")
    assert drp.silicon_ref == "renesas:rzv2n:n44"
    assert drp.accel_config == ""              # drpai has no vela-style accel-config


def test_resolve_targets_for_v2m101_folds_in_on_module_deepx():
    # E1M-V2M101 -> host renesas:rzv2n:n44 (drpai) + on-module DEEPX DX-M1
    # (deepx:dx:m1, found via variants[].alp_module_skus) + cpu.
    specs = resolve_targets("E1M-V2M101", metadata_root=_META)
    by = {(s.backend, s.silicon_ref) for s in specs}
    assert ("drpai", "renesas:rzv2n:n44") in by
    assert ("deepx_dxm1", "deepx:dx:m1") in by          # discrete accelerator folded in
    assert ("cpu", "*") in by
    deepx = next(s for s in specs if s.backend == "deepx_dxm1")
    assert deepx.accel_config == ""


def test_resolve_targets_v2n101_has_no_discrete_deepx():
    # regression: V2N101 has no on-module DEEPX, must NOT gain a deepx target
    specs = resolve_targets("E1M-V2N101", metadata_root=_META)
    assert all(s.backend != "deepx_dxm1" for s in specs)


def test_resolve_targets_for_aen801_yields_three_ethos_configs_plus_cpu():
    # E1M-AEN801 -> alif:ensemble:e8 -> Ethos-U85 (generative) + U55-HP + U55-HE
    # + cpu.  This is the part arriving for bench bring-up; AEN701 (E7) carries
    # only the U55 pair, so the U85 path was previously unexercised in CI.
    specs = resolve_targets("E1M-AEN801", metadata_root=_META)
    by = {(s.backend, s.accel_config) for s in specs}
    assert ("ethos_u", "ethos-u85-256") in by      # E8-only generative NPU
    assert ("ethos_u", "ethos-u55-256") in by
    assert ("ethos_u", "ethos-u55-128") in by
    assert ("cpu", "") in by
    for s in specs:
        if s.backend == "ethos_u":
            assert s.silicon_ref == "alif:ensemble:e8"


def test_resolve_targets_aen801_has_three_distinct_ethos_configs():
    specs = resolve_targets("E1M-AEN801", metadata_root=_META)
    ethos = {s.accel_config for s in specs if s.backend == "ethos_u"}
    assert ethos == {"ethos-u85-256", "ethos-u55-256", "ethos-u55-128"}


# Vela's documented --accelerator-config choices (ethos-u-vela 5.1.0; the same
# set Vela prints when handed a bogus config).  A resolved ethos_u accel_config
# outside this set would be silently rejected by Vela at compile/bench time, so
# this closes the metadata({npu_type}-{mac_per_cycle}) -> Vela contract.
_VELA_ACCEL_CONFIGS = {
    "ethos-u55-32", "ethos-u55-64", "ethos-u55-128", "ethos-u55-256",
    "ethos-u65-256", "ethos-u65-512",
    "ethos-u85-128", "ethos-u85-256", "ethos-u85-512",
    "ethos-u85-1024", "ethos-u85-2048",
}


def test_every_aen_ethos_accel_config_is_a_valid_vela_choice():
    skus = sorted(p.stem for p in (_META / "e1m_modules").glob("E1M-AEN*.yaml"))
    assert skus, "expected at least one E1M-AEN* SoM preset"
    checked = 0
    for sku in skus:
        for s in resolve_targets(sku, metadata_root=_META):
            if s.backend == "ethos_u":
                checked += 1
                assert s.accel_config in _VELA_ACCEL_CONFIGS, (
                    f"{sku}: resolved accel_config {s.accel_config!r} is not a valid "
                    f"Vela --accelerator-config choice (bad mac_per_cycle in the SoC JSON?)")
    assert checked, "expected at least one ethos_u target across the AEN SoM presets"
