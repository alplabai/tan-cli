# SPDX-License-Identifier: Apache-2.0
"""`npu_toolchain.vela` (alp-sdk #1470) -> `TargetSpec.vela_*`, on a SYNTHETIC
metadata tree.

Separate from `test_targets.py` on purpose: that module is skipped wholesale
without a bound `ALP_SDK_ROOT`, because its assertions are about the real
committed SoM presets and SoC JSON. These are about the MECHANISM, and the
mechanism has a branch no shipped SoC spec exercises today -- every Alif
Ensemble part sets `system_config_requires_vendor_config: true` (its tuned
sections live only in the proprietary `ensemble_vela.ini`) and the NXP i.MX 93
names no `system_config` at all, so `vela_system_config` resolves to None for
every part currently in metadata. Pinned here against a spec written for the
test, so the pass-it-through branch is proved rather than left as dead code
that will be discovered broken by the first SoC spec that needs it. Building
the tree here also means these run in the bare CI install, with no SDK bound.
"""
import json

import pytest

from tan.model.targets import resolve_targets

_SKU = "E1M-FAKE001"
_SILICON = "fakevendor:fakefamily:fakepart"


def _metadata_root(tmp_path, vela_block: dict | None) -> object:
    """A metadata/ tree with exactly what `resolve_targets` reads: one SoM
    preset naming one SoC, and that SoC's spec with a single Ethos-U NPU."""
    root = tmp_path / "metadata"
    (root / "e1m_modules").mkdir(parents=True)
    (root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        f"sku: {_SKU}\nsilicon: {_SILICON}\n", encoding="utf-8")
    vendor, family, part = _SILICON.split(":")
    soc_dir = root / "socs" / vendor / family
    soc_dir.mkdir(parents=True)
    soc: dict = {"ref": _SILICON, "npus": [{"type": "ethos-u85", "mac_per_cycle": 256}]}
    if vela_block is not None:
        soc["npu_toolchain"] = {"vela": vela_block}
    (soc_dir / f"{part}.json").write_text(json.dumps(soc), encoding="utf-8")
    return root


def _ethos_u(tmp_path, vela_block):
    specs = resolve_targets(_SKU, metadata_root=_metadata_root(tmp_path, vela_block))
    ethos = [s for s in specs if s.backend == "ethos_u"]
    assert len(ethos) == 1, "fixture declares exactly one Ethos-U NPU"
    return ethos[0]


def test_a_system_config_that_needs_no_vendor_config_reaches_the_target(tmp_path):
    """The branch no shipped spec exercises yet: a System_Config that IS one of
    Arm's built-ins (`Ethos_U85_SYS_Flash_High` ships in vela 5.1.0's own
    vela.ini) is safe to pass with no `--config`, so it must flow through."""
    spec = _ethos_u(tmp_path, {
        "memory_mode": "Sram_Only",
        "system_config": "Ethos_U85_SYS_Flash_High",
        "system_config_requires_vendor_config": False,
        "source": "vendors/fake/README.md:1-2",
    })
    assert spec.vela_memory_mode == "Sram_Only"
    assert spec.vela_system_config == "Ethos_U85_SYS_Flash_High"


def test_a_vendor_gated_system_config_is_dropped_and_the_memory_mode_is_kept(tmp_path):
    """`system_config_requires_vendor_config: true` means the section exists
    only in a file tan cannot pass, and naming it is vela rc=1 (`Section
    System_Config.Ethos_U85_SRAM_Only not found in Vela config file`), not a
    degradation. The memory mode beside it is an Arm built-in and survives --
    it is the flag that actually fixes the footprint."""
    spec = _ethos_u(tmp_path, {
        "memory_mode": "Sram_Only",
        "system_config": "Ethos_U85_SRAM_Only",
        "system_config_requires_vendor_config": True,
        "vendor_config_filename": "ensemble_vela.ini",
        "source": "examples/aen/aen-npu-inference-alp/CMakeLists.txt:42-43",
    })
    assert spec.vela_memory_mode == "Sram_Only"
    assert spec.vela_system_config is None


def test_an_undeclared_vendor_requirement_withholds_the_system_config(tmp_path):
    """tan-cli#789 review MINOR 5: the guard must fail CLOSED.

    Reading the key with `.get()` treated an ABSENT
    `system_config_requires_vendor_config` as "no vendor config needed" and let
    the name onto vela's command line. The defence was that alp-sdk's SoC
    schema declares the key `required` -- but that constraint is enforced in
    the OTHER repo, against whatever checkout `ALP_SDK_ROOT` happens to point
    at (any tag or branch on the user's disk), which is precisely the cross-repo
    drift class `tests/gates/test_planner_relocation_freshness.py` exists for.
    Here the consequence is not a stale hash: a `system_config` that turns out
    to need a vendor `.ini` is vela rc=1, `Section System_Config.<name> not
    found in Vela config file`, i.e. a hard build failure rather than a
    degradation. Only an explicit `False` is a promise; omission is not."""
    spec = _ethos_u(tmp_path, {
        "memory_mode": "Sram_Only",
        "system_config": "Ethos_U85_SRAM_Only",
        # `system_config_requires_vendor_config` deliberately ABSENT
        "source": "vendors/fake/README.md:1-2",
    })
    assert spec.vela_memory_mode == "Sram_Only"      # the Arm built-in still flows
    assert spec.vela_system_config is None           # ... the unvouched name does not


def test_an_absent_system_config_is_nothing_to_pass(tmp_path):
    """Missing means "this part names none", NEVER "no vendor config needed"
    -- the shape every shipped Alif and NXP spec is in today."""
    spec = _ethos_u(tmp_path, {
        "memory_mode": "Shared_Sram",
        "system_config_requires_vendor_config": False,
        "source": "vendors/nxp-imx93/README.md:103-108",
    })
    assert spec.vela_memory_mode == "Shared_Sram"
    assert spec.vela_system_config is None


@pytest.mark.parametrize("block", [None, {}, {"source": "x:1-2"}])
def test_a_soc_spec_with_no_vela_profile_resolves_to_no_flags(tmp_path, block):
    """A spec predating the block (or carrying one with no `memory_mode`)
    yields None/None -- i.e. exactly today's flagless vela invocation, never a
    guessed profile."""
    spec = _ethos_u(tmp_path, block)
    assert spec.vela_memory_mode is None
    assert spec.vela_system_config is None
