# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.som_metadata.resolve_capabilities` / `som_unpopulated_capabilities`.

This is the port of alp-sdk's `scripts/alp_project_loader.py::resolve_capabilities`
(relocated "substantially as-is" -- see `som_metadata.py`'s module docstring), and
it had ZERO dedicated tests in this tree before this file: a grep of `python/tests/`
for "capabilit" hit only the whole-CLI parity harness (`tests/parity/oracle.py` +
`tests/parity/test_oracle_parity.py`), neither of which exercises capability
resolution itself.

Ported from the oracle's OWN suite (`<sdk>/tests/scripts/test_resolve_capabilities.py`,
read at `origin/dev`), not invented against today's port -- a test that only pins
current output proves nothing if the port already diverged from the source it claims
to be a verbatim move of. Compared byte-for-byte against `alp_project_loader.py` at
`origin/dev` while writing this file: **no divergence found** in any of the three
functions' BODIES. Docstrings: `resolve_capabilities`'s is byte-identical to its
alp-sdk counterpart; `resolve_soc_path`'s dropped the oracle's issue-#997/#1004
copy-site inventory (the migration-target list); `som_unpopulated_capabilities`'s
reworded its module references for the new location (`the loader` -> `this module`,
`alp_orchestrate/kconfig.py` -> `kconfig.py`, `the header generator` -> `alp-sdk's
header generator`) rather than carrying them verbatim.

`crates/` (the Rust oracle) carries NO equivalent of THIS resolution (SoC-JSON +
SoM-preset merge with `unpopulated` restriction) -- grepped for "capabilit" and
"unpopulated" across the whole workspace (`grep -ril -i capabilit crates/`,
`grep -ril -i unpopulated crates/`; `unpopulated` hits zero files anywhere).
The "capabilit" hits that do exist are unrelated: `debug/context.rs`'s
`DebugRuntimeCapabilities` (host-tooling probe for `tan doctor`), `pinmux.rs`'s
`pinmux-capability-v1` table (E1M pad -> silicon function, no SoC/SoM merge),
and `sdk_catalogue/parse.rs`, which reads a SoM preset's own `capabilities:`
key verbatim into a `BTreeMap` (`sdk_catalogue/derive.rs`'s backend-selection
`som.capabilities.get("deepx_dxm1")`) without ever touching the SoC JSON or
`silicon_capabilities.unpopulated` -- the merge/restriction this file tests is
simply absent, not performed some other way. `tan generate`/`tan build` shell
`alp_project.py` for any capability-bearing emit (`crates/tan-cli/src/commands/
generate.rs`); the Rust CLI never re-derives capabilities itself. So the real
oracle for this logic is alp-sdk's own Python, which `som_metadata.py` is a
relocation of -- this file grounds against THAT (both via synthetic fixtures
mirroring the oracle's own test shapes, and via the real RZ/V2N n44 + Alif
Ensemble e7/e8 SoC JSON at the bound `ALP_SDK_ROOT`).

Requires a bound alp-sdk checkout (`ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT`) for the same
reason `tests/parity/test_planner_emit_parity.py` and `tests/core/test_kconfig_symbols.py`
do: `tan.planner`'s `__init__` eagerly reads `metadata/registries/*` at import time, so
`tan.planner.som_metadata` cannot even be imported unbound. Skipped, loudly, without
one.
"""

from __future__ import annotations

import json
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

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind "
           "a root and import (same requirement as the parity suite)",
)


@pytest.fixture(scope="module")
def sm():
    from tan.planner_root import bind_sdk_root
    assert SDK is not None
    bind_sdk_root(SDK)
    from tan.planner import som_metadata
    return som_metadata


@pytest.fixture(scope="module")
def real_metadata_root() -> Path:
    assert SDK is not None
    return SDK / "metadata"


# ---------------------------------------------------------------------------
# Synthetic metadata root helper -- mirrors the oracle's own
# `_make_soc_json` in `tests/scripts/test_resolve_capabilities.py`.
# ---------------------------------------------------------------------------

def _make_soc_json(tmp_path: Path, vendor: str, family: str, part: str,
                   capabilities: dict) -> Path:
    soc_dir = tmp_path / "socs" / vendor / family
    soc_dir.mkdir(parents=True, exist_ok=True)
    soc_file = soc_dir / f"{part}.json"
    soc_data = {
        "soc_spec_version": 1,
        "ref": f"{vendor}:{family}:{part}",
        "vendor": vendor,
        "family": family,
        "part": part,
        "cores": [{"id": "m55_hp", "type": "cortex-m55", "count": 1}],
        "npus": [],
        "peripherals": {},
        "capabilities": capabilities,
    }
    soc_file.write_text(json.dumps(soc_data), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Synthetic-fixture cases (ported from the oracle's own suite)
# ---------------------------------------------------------------------------

def test_soc_caps_appear_in_merged_result(sm, tmp_path):
    """SoC-side keys are present in the merged result when the SoM has no caps."""
    soc_caps = {"drp_ai": True, "neon": True, "helium_mve": False}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {"sku": "E1M-TEST", "silicon": "testvendor:testfam:testpart"}

    result = sm.resolve_capabilities(preset, meta)

    assert result["drp_ai"] is True
    assert result["neon"] is True
    assert result["helium_mve"] is False


def test_som_override_wins_over_soc_default(sm, tmp_path):
    """SoM-declared value wins over the SoC default for the same key -- the
    exact V2N `cau: true`-over-silicon's-`cau: false` shape."""
    soc_caps = {"cau": False, "neon": True}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {
        "sku": "E1M-TEST",
        "silicon": "testvendor:testfam:testpart",
        "capabilities": {"cau": True},
    }

    result = sm.resolve_capabilities(preset, meta)

    assert result["cau"] is True
    assert result["neon"] is True  # unrelated SoC key untouched


def test_som_only_extension_keys_survive(sm, tmp_path):
    """SoM-only keys (add-on chips the SoC JSON has no concept of) pass
    through even though the SoC lacks them -- 'a board declaring a
    capability the SoC lacks'."""
    soc_caps = {"drp_ai": True}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {
        "sku": "E1M-TEST",
        "silicon": "testvendor:testfam:testpart",
        "capabilities": {
            "optiga_trust_m": True,
            "tmu_cordic": True,
            "tmu_fft": True,
            "tmu_fac": True,
        },
    }

    result = sm.resolve_capabilities(preset, meta)

    assert result["optiga_trust_m"] is True
    assert result["tmu_cordic"] is True
    assert result["tmu_fft"] is True
    assert result["tmu_fac"] is True
    assert result["drp_ai"] is True


def test_missing_silicon_returns_only_som_caps(sm, tmp_path):
    """No `silicon:` at all -- a valid, if incomplete, preset -- must not crash."""
    preset = {"sku": "E1M-TEST", "capabilities": {"optiga_trust_m": True}}
    assert sm.resolve_capabilities(preset, tmp_path) == {"optiga_trust_m": True}


def test_both_sides_absent_returns_empty(sm, tmp_path):
    """The empty/absent-capability case: no silicon, no SoM caps -> `{}`."""
    assert sm.resolve_capabilities({}, tmp_path) == {}


def test_unknown_silicon_ref_returns_only_som_caps(sm, tmp_path):
    """A `silicon:` ref that resolves to a path (3 colon-separated parts) but
    names no file on disk -- soft-fails to `soc_caps = {}`, does not raise."""
    preset = {
        "sku": "E1M-TEST",
        "silicon": "unknown:vendor:part",
        "capabilities": {"custom_key": True},
    }
    assert sm.resolve_capabilities(preset, tmp_path) == {"custom_key": True}


def test_unpopulated_restriction_forces_flag_false(sm, tmp_path):
    """`silicon_capabilities.unpopulated` forces a truthy silicon flag to False."""
    soc_caps = {"gpu2d": True, "dave2d": True, "neon": True}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {
        "sku": "E1M-TEST",
        "silicon": "testvendor:testfam:testpart",
        "silicon_capabilities": {"unpopulated": ["gpu2d", "dave2d"]},
    }

    result = sm.resolve_capabilities(preset, meta)

    assert result["gpu2d"] is False
    assert result["dave2d"] is False
    assert result["neon"] is True  # unlisted silicon caps keep the full value


def test_unpopulated_restriction_forces_count_to_zero(sm, tmp_path):
    """Count-style silicon caps (e.g. `ethos_u55_count`) restrict to `0`, not
    `False`, so a downstream `> 0` presence check keeps its integer semantics
    -- the typing decision hinges on the SoC-side value's type, not the
    restricted name's spelling."""
    soc_caps = {"ethos_u55_count": 2, "ethos_u85_count": 1, "helium_mve": True}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {
        "sku": "E1M-TEST",
        "silicon": "testvendor:testfam:testpart",
        "silicon_capabilities": {"unpopulated": ["ethos_u85_count"]},
    }

    result = sm.resolve_capabilities(preset, meta)

    assert result["ethos_u85_count"] == 0
    assert isinstance(result["ethos_u85_count"], int)
    assert not isinstance(result["ethos_u85_count"], bool)
    assert result["ethos_u55_count"] == 2
    assert result["helium_mve"] is True


def test_unpopulated_naming_a_key_absent_from_soc_caps_still_forces_false(sm, tmp_path):
    """A restriction can name a key the SoC JSON never declared at all (not
    merely `False`/`0` -- genuinely absent). `base = soc_caps.get(name)` is
    then `None`; `isinstance(None, int)` is `False`, so the else branch fires
    and the key is ADDED to the result as `False` rather than left out or
    erroring. This is real behaviour of the shipped function, not a case a
    schema gate would let through in practice (`validate_metadata.py` cross-
    checks `unpopulated` against the SoC's own capability set) -- pinned here
    because the function itself does not enforce that guard."""
    soc_caps = {"neon": True}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {
        "sku": "E1M-TEST",
        "silicon": "testvendor:testfam:testpart",
        "silicon_capabilities": {"unpopulated": ["not_a_real_capability"]},
    }

    result = sm.resolve_capabilities(preset, meta)

    assert result["not_a_real_capability"] is False
    assert result["neon"] is True


def test_no_restriction_field_keeps_full_silicon_set(sm, tmp_path):
    """Absence of `silicon_capabilities:` = the full silicon capability set --
    the zero-behaviour-change default for every unrestricted SKU."""
    soc_caps = {"gpu2d": True, "neon": True}
    meta = _make_soc_json(tmp_path, "testvendor", "testfam", "testpart", soc_caps)
    preset = {"sku": "E1M-TEST", "silicon": "testvendor:testfam:testpart"}
    restricted = dict(preset, silicon_capabilities={"unpopulated": ["gpu2d"]})

    assert sm.resolve_capabilities(preset, meta) == {"gpu2d": True, "neon": True}
    assert sm.resolve_capabilities(restricted, meta) == {"gpu2d": False, "neon": True}


def test_som_unpopulated_capabilities_accessor(sm):
    """The shared accessor: `[]` for absent/malformed blocks, the transcribed
    list otherwise -- every conditional branch in the function."""
    assert sm.som_unpopulated_capabilities({}) == []
    assert sm.som_unpopulated_capabilities({"silicon_capabilities": None}) == []
    # block present but not a dict
    assert sm.som_unpopulated_capabilities({"silicon_capabilities": "gpu2d"}) == []
    # names present but not a list
    assert sm.som_unpopulated_capabilities(
        {"silicon_capabilities": {"unpopulated": None}}) == []
    assert sm.som_unpopulated_capabilities(
        {"silicon_capabilities": {"unpopulated": "gpu2d"}}) == []
    assert sm.som_unpopulated_capabilities(
        {"silicon_capabilities": {"unpopulated": ["gpu2d", "dave2d"]}}
    ) == ["gpu2d", "dave2d"]
    # non-string entries are coerced via str(), not rejected
    assert sm.som_unpopulated_capabilities(
        {"silicon_capabilities": {"unpopulated": [1, "gpu2d"]}}
    ) == ["1", "gpu2d"]


# ---------------------------------------------------------------------------
# `resolve_soc_path` -- the `silicon:` resolution every case above goes
# through, tested directly for its own branches.
# ---------------------------------------------------------------------------

def test_resolve_soc_path_branches(sm, tmp_path):
    assert sm.resolve_soc_path(None, tmp_path) is None
    assert sm.resolve_soc_path("", tmp_path) is None
    assert sm.resolve_soc_path("only:two", tmp_path) is None
    assert sm.resolve_soc_path("way:too:many:parts", tmp_path) is None
    # Well-formed and 3-part: a Path is built WITHOUT checking existence.
    got = sm.resolve_soc_path("vendor:family:part", tmp_path)
    assert got == tmp_path / "socs" / "vendor" / "family" / "part.json"
    assert not got.exists()


# ---------------------------------------------------------------------------
# Real-metadata cases, grounded against the bound alp-sdk checkout --
# "a capability present on one silicon variant and absent on another" and
# "a board declaring a capability the SoC lacks", using real SKU/SoC data
# rather than synthetic stand-ins.
# ---------------------------------------------------------------------------

def test_v2n101_real_soc_drp_ai_true_and_som_only_extensions_survive(sm, real_metadata_root):
    """RZ/V2N n44: SoC-side `drp_ai`/`neon`/`emmc_dma` come through unchanged.
    `n44.json`'s `capabilities` block declares only those three keys -- no
    `cau`, no `quadspi_dma` -- so the SoM's `cau`/`quadspi_dma`/`optiga_trust_m`/
    `tmu_*` keys are SoM-only additions passed through, same as the synthetic
    SoM-only-keys case above and the `deepx_dxm1` case below, NOT an override
    of an existing SoC-side `False` (there is none to override). Mirrors
    `metadata/e1m_modules/E1M-V2N101.yaml`'s real `capabilities:` block."""
    preset = {
        "sku": "E1M-V2N101",
        "silicon": "renesas:rzv2n:n44",
        "capabilities": {
            "cau": True,
            "optiga_trust_m": True,
            "quadspi_dma": True,
            "tmu_cordic": True,
            "tmu_fft": True,
            "tmu_fac": True,
        },
    }
    result = sm.resolve_capabilities(preset, real_metadata_root)

    soc_caps = json.loads(
        (real_metadata_root / "socs" / "renesas" / "rzv2n" / "n44.json")
        .read_text(encoding="utf-8")
    ).get("capabilities", {})
    assert "cau" not in soc_caps
    assert "quadspi_dma" not in soc_caps

    assert result["drp_ai"] is True
    assert result["neon"] is True
    assert result["emmc_dma"] is True
    assert result["cau"] is True
    assert result["quadspi_dma"] is True
    assert result["optiga_trust_m"] is True
    assert result["tmu_cordic"] is True
    assert result["tmu_fft"] is True
    assert result["tmu_fac"] is True


def test_v2m101_real_soc_deepx_dxm1_is_a_som_only_addon(sm, real_metadata_root):
    """E1M-V2M101 (RZ/V2N + DEEPX DX-M1): `deepx_dxm1` is a capability the
    RZ/V2N silicon itself has no concept of at all -- the on-module NPU
    reaches the SoC over PCIe, not through anything `n44.json`'s
    `capabilities` block declares. This is the real-metadata instance of 'a
    board declaring a capability the SoC lacks': it must survive the merge
    untouched, same as the synthetic SoM-only-keys case above."""
    preset = {
        "sku": "E1M-V2M101",
        "silicon": "renesas:rzv2n:n44",
        "capabilities": {"deepx_dxm1": True, "cau": True},
    }
    result = sm.resolve_capabilities(preset, real_metadata_root)

    assert result["deepx_dxm1"] is True
    assert "deepx_dxm1" not in json.loads(
        (real_metadata_root / "socs" / "renesas" / "rzv2n" / "n44.json")
        .read_text(encoding="utf-8")
    ).get("capabilities", {})


def test_ethos_u85_present_on_e8_absent_on_e7(sm, real_metadata_root):
    """The real cross-silicon-variant case: `ethos_u85_count` is declared on
    the Alif Ensemble E8 SoC JSON but genuinely ABSENT (not `0`) from E7's --
    E7 shipped before the second Ethos-U NPU existed. `.get()` on the E7
    result must miss the key entirely, not default it to `0`."""
    e7 = sm.resolve_capabilities(
        {"sku": "E1M-AEN701", "silicon": "alif:ensemble:e7"}, real_metadata_root)
    e8 = sm.resolve_capabilities(
        {"sku": "E1M-AEN801", "silicon": "alif:ensemble:e8"}, real_metadata_root)

    assert "ethos_u85_count" not in e7
    assert e7.get("ethos_u85_count") is None
    assert e8["ethos_u85_count"] == 1
    # Both variants keep the single Ethos-U55 pair.
    assert e7["ethos_u55_count"] == 2
    assert e8["ethos_u55_count"] == 2


def test_aen701_real_soc_cau_stays_false_with_no_bridge(sm, real_metadata_root):
    """E7 has no CAU and AEN701 has no bridge to add one, so `cau` stays the
    silicon's own `False` -- the negative-space companion to the V2N
    override case above: no SoM key present means the SoC default survives
    unchanged, not '`false` because nothing said otherwise'."""
    preset = {
        "sku": "E1M-AEN701",
        "silicon": "alif:ensemble:e7",
        "capabilities": {"optiga_trust_m": True},
    }
    result = sm.resolve_capabilities(preset, real_metadata_root)

    assert result["helium_mve"] is True
    assert result["neon"] is True
    assert result["cryptocell"] is True
    assert result["xspi_dma"] is True
    assert result["cau"] is False
    assert result["optiga_trust_m"] is True
