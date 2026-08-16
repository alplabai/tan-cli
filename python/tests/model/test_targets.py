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
from tests.conftest import needs_sdk_vela_profile, sdk_root

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


@needs_sdk_vela_profile
def test_an_alif_ethos_u_target_carries_the_builtin_memory_mode_but_not_the_vendor_system_config():
    specs = resolve_targets("E1M-AEN801", metadata_root=_META)
    u85 = [s for s in specs if s.accel_config == "ethos-u85-256"]
    assert u85, "E1M-AEN801 must resolve an ethos-u85-256 target"
    assert u85[0].vela_memory_mode == "Sram_Only"
    # Ethos_U85_SRAM_Only lives only in the proprietary ensemble_vela.ini;
    # passing it without --config is vela rc=1 "Section ... not found".
    assert u85[0].vela_system_config is None
    # ... and the file that WOULD define it is named, because e8.json declares
    # it -- that is what a footprint refusal points a reader at.
    assert u85[0].vela_vendor_config_filename == "ensemble_vela.ini"
    # No Alif spec names a System_Config at all (an Alif one is per core
    # subsystem, not per die), so there is nothing for `--config` to complete.
    assert u85[0].vela_vendor_system_config is None


@needs_sdk_vela_profile
def test_an_nxp_ethos_u_target_carries_its_own_memory_mode():
    specs = resolve_targets("E1M-NX9101", metadata_root=_META)
    u65 = [s for s in specs if s.accel_config == "ethos-u65-256"]
    assert u65, "E1M-NX9101 must resolve an ethos-u65-256 target"
    assert u65[0].vela_memory_mode == "Shared_Sram"
    # imx93.json declares `system_config_requires_vendor_config: false` and no
    # filename -- so a refusal on this part names no vendor file at all.
    assert u65[0].vela_vendor_config_filename is None


def test_the_real_soc_specs_answer_the_dram_question_per_part():
    """The EVIDENCE behind the zero-SRAM refusal, read off the committed specs
    rather than asserted in prose (alp-sdk #1470 Task 4).

    `metadata/socs/alif/ensemble/e8.json`'s `external_memory_interfaces` lists
    exactly `HexSPI` and `SD/eMMC`; `nxp/imx9/imx93.json` lists `LPDDR4/4X`,
    `FlexSPI` and `SD/eMMC`; `renesas/rzv2n/n44.json` lists `LPDDR4/4X`. So
    "vela put the working set in DRAM and this part has no DRAM interface" is
    true for the Alif parts and FALSE for the other two -- which is exactly why
    it is resolved per part instead of stated once in a comment.

    Not `@needs_sdk_vela_profile`-gated: `external_memory_interfaces` long
    predates `npu_toolchain.vela`, so any bound checkout can answer this."""
    for sku in ("E1M-AEN801", "E1M-AEN701"):
        for spec in resolve_targets(sku, metadata_root=_META):
            if spec.silicon_ref != "*":
                assert spec.soc_declares_dram is False, f"{sku}/{spec.silicon_ref}"
    for sku in ("E1M-NX9101", "E1M-V2N101"):
        for spec in resolve_targets(sku, metadata_root=_META):
            if spec.silicon_ref != "*":
                assert spec.soc_declares_dram is True, f"{sku}/{spec.silicon_ref}"


def test_a_non_vela_target_never_carries_a_vela_profile():
    """The profile is a vela flag, so it must not ride on a target no vela
    invocation ever sees. `E1M-V2M101` resolves a DRP-AI target and a discrete
    DEEPX one alongside `cpu`; a memory mode on any of those would be handed to
    an adapter that has no such flag, and would read as silicon fact about a
    compiler that never ran. Same for the vendor half of the profile, whose
    only legal use is a vela `--config`."""
    for sku in ("E1M-V2M101", "E1M-AEN801"):
        for spec in resolve_targets(sku, metadata_root=_META):
            if spec.backend == "ethos_u":
                continue
            assert spec.vela_memory_mode is None, f"{sku}/{spec.backend}"
            assert spec.vela_system_config is None, f"{sku}/{spec.backend}"
            assert spec.vela_vendor_system_config is None, f"{sku}/{spec.backend}"
            assert spec.vela_vendor_config_filename is None, f"{sku}/{spec.backend}"


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
