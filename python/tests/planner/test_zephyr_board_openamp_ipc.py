# SPDX-License-Identifier: Apache-2.0
"""E1M-V2M101's `.dts` carries the OpenAMP/MHU-B carve-out, and a preset that
clears `topology.m33_sm.openamp_ipc` loses it (hand-ported from alp-sdk#1948,
`a2228e2e8`).

E1M-V2M101 is the same RZ/V2N die and MHU-B as E1M-V2N101, so its preset sets
the flag too. No committed board sets it false any more, so the shorter path
is exercised on a preset with the flag cleared -- a generator that could only
emit the longer file would make the gate vacuous.

Real-SDK-gated: both tests read the bound checkout's `metadata/`.
"""

from __future__ import annotations

import copy

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read the bound metadata/ tree. A SKIP about "
           "the missing root, not a pass.",
)

_DTS = "alp_e1m_v2m101_m33_sm/alp_e1m_v2m101_m33_sm_r9a09g056n48gbg_cm33.dts"


def _emit():
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.zephyr_board import emit_zephyr_board

    return emit_zephyr_board("E1M-V2M101", "m33_sm", METADATA_ROOT)[_DTS]


def test_v2m_dts_carries_the_openamp_block():
    dts = _emit()
    assert "openamp_shm: memory@9f700000" in dts
    assert "mbox1: mhu@" in dts


def test_openamp_ipc_false_drops_the_block(monkeypatch):
    import tan.planner.zephyr_board as zb

    real = zb._resolve_sku

    def cleared(sku, root):
        preset = copy.deepcopy(real(sku, root))
        preset["topology"]["m33_sm"]["openamp_ipc"] = False
        return preset

    monkeypatch.setattr(zb, "_resolve_sku", cleared)
    dts = _emit()
    assert "OpenAMP" not in dts
    assert "reserved-memory" not in dts
    assert "mbox1: mhu@" not in dts
    assert "No &canfd node" not in dts
