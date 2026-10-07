# SPDX-License-Identifier: Apache-2.0
"""`resolve_capabilities` publishes `gpu2d` from the SKU's silicon variant
(`variants[].optional_features.gpu_mali_g31`), hand-ported from alp-sdk#2678.

The SoC-level `capabilities` block cannot carry it: four of the eight RZ/V2N
dies are fused without the Mali-G31. Pure-logic test over a synthetic
metadata tree; it is gated on a bound SDK only because `tan.planner` cannot
be imported unbound.
"""

from __future__ import annotations

import json

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- tan.planner cannot be imported unbound. A SKIP about "
           "the missing root, not a pass.",
)


def resolve_capabilities(preset, root):
    from tan.planner.som_metadata import resolve_capabilities as real

    return real(preset, root)


def _tree(tmp_path, *, mali):
    soc = {"capabilities": {"cau": False},
           "variants": [{"order_code": "X1", "optional_features": mali}]}
    (tmp_path / "socs").mkdir()
    (tmp_path / "socs" / "x.json").write_text(json.dumps(soc), encoding="utf-8")
    return tmp_path


@pytest.fixture
def stub_variant(monkeypatch):
    import tan.planner.som_metadata as sm

    def install(variant):
        monkeypatch.setattr(sm, "_resolve_silicon_variant",
                            lambda preset, root: variant)
    return install


@pytest.mark.parametrize("fused, expected", [(True, True), (False, False)])
def test_gpu2d_follows_the_die_variant(tmp_path, stub_variant, fused, expected):
    stub_variant({"optional_features": {"gpu_mali_g31": fused}})
    caps = resolve_capabilities({"silicon": "nope"}, tmp_path)
    assert caps["gpu2d"] is expected


def test_gpu2d_absent_when_variant_does_not_declare_it(tmp_path, stub_variant):
    stub_variant({"optional_features": {"isp_mali_c55": True}})
    assert "gpu2d" not in resolve_capabilities({"silicon": "nope"}, tmp_path)
    stub_variant(None)
    assert "gpu2d" not in resolve_capabilities({"silicon": "nope"}, tmp_path)


def test_die_fact_overrides_a_som_declared_gpu2d(tmp_path, stub_variant):
    stub_variant({"optional_features": {"gpu_mali_g31": False}})
    caps = resolve_capabilities(
        {"silicon": "nope", "capabilities": {"gpu2d": True}}, tmp_path)
    assert caps["gpu2d"] is False
