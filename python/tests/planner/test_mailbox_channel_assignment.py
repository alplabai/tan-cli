# SPDX-License-Identifier: Apache-2.0
"""Mailbox channel assignment for `ipc:` entries (tan-cli#1487; ported from
alp-sdk#2822's `tests/scripts/test_orchestrate_mailbox_channel_assignment.py`).

An entry not named after a `mailbox.channels[].reserved_for` tag used to fall
back to channel 0 -- the channel reserved for `alp_default_rpmsg` -- so a
second ipc entry silently aliased the default rpmsg link.  Now: exact
reservation name wins, else the lowest unclaimed `reserved_for: app` channel,
else the entry lands blocked.  Channel 0 is never handed out by fallback.

E1M-V2N101 declares channels 0 alp_default_rpmsg, 1 app, 2 app, 3 power_mgmt.

Real-SDK-gated: needs E1M-V2N101's real SoM preset from a bound alp-sdk
checkout.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner requires a bound root and "
           "E1M-V2N101's real SoM preset. A SKIP about the missing root, "
           "not a pass.",
)


def _write_board(tmp: Path, body: str) -> Path:
    path = tmp / "board.yaml"
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return path


def _resolve(tmp_path, ipc_body):
    from tan.planner import load_board_yaml, resolve_carve_outs

    ipc_body = ipc_body.strip()
    path = _write_board(tmp_path, f"""
    name: test-v2n101-mbox-channels
    som:
      sku: E1M-V2N101
      hw_rev: r1

    cores:
      a55_cluster:
        os: yocto
        app: ./linux
        image: alp-image-edge
      m33_sm:
        os: zephyr
        app: ./m33

    ipc:
    {ipc_body}
    """)
    return {c.name: c for c in resolve_carve_outs(load_board_yaml(path))}


def _ent(name, kind="raw_shmem"):
    return (f"- {{name: {name}, kind: {kind}, "
            f"endpoints: [a55_cluster, m33_sm], carve_out_kb: 64}}\n    ")


def test_reserved_name_keeps_its_channel(tmp_path):
    parts = _resolve(tmp_path, _ent("alp_default_rpmsg", "rpmsg"))
    assert parts["alp_default_rpmsg"].status == "ok"
    assert parts["alp_default_rpmsg"].mailbox_channel == 0


def test_unreserved_entry_never_aliases_channel_zero(tmp_path):
    parts = _resolve(tmp_path, _ent("alp_default_rpmsg", "rpmsg") + _ent("zz_extra"))
    assert parts["zz_extra"].status == "ok", parts["zz_extra"].reason
    assert parts["zz_extra"].mailbox_channel == 1
    assert parts["alp_default_rpmsg"].mailbox_channel == 0


def test_unreserved_entries_sorted_before_default_do_not_take_its_channel(tmp_path):
    parts = _resolve(tmp_path, _ent("aaa_first") + _ent("alp_default_rpmsg", "rpmsg"))
    assert parts["alp_default_rpmsg"].mailbox_channel == 0
    assert parts["aaa_first"].mailbox_channel == 1


def test_app_channels_exhausted_blocks_with_clear_reason(tmp_path):
    parts = _resolve(tmp_path, _ent("one") + _ent("two") + _ent("three"))
    chans = sorted(p.mailbox_channel for p in parts.values() if p.status == "ok")
    assert chans == [1, 2]
    blocked = [p for p in parts.values() if p.status == "blocked"]
    assert len(blocked) == 1
    assert blocked[0].mailbox_channel == 0  # blocked stub, not a real assignment
    assert "no mailbox channel" in (blocked[0].reason or "")
    assert f"'{blocked[0].name}'" in (blocked[0].reason or "")
