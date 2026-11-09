# SPDX-License-Identifier: Apache-2.0
"""Hermetic ports from the alp-sdk `84a6e0d2` -> `64481b8e` planner re-sync
(tan-cli#1525): the E1M CAN -> Linux netdev map (alp-sdk#2352) and the AEN
`alp,wired-lpgpio-pads` property (alp-sdk#2831).  No alp-sdk checkout needed: a synthetic planner root is
bound (`bound_sdk_root`), so these run on every PR.
"""
from __future__ import annotations

import pytest

from tests.planner._baremetal_support import bound_sdk_root  # noqa: F401

@pytest.fixture
def lo():
    from tan.planner import linux_ownership

    return linux_ownership


@pytest.fixture
def zb():
    from tan.planner import zephyr_board

    return zephyr_board


def _can_fixture(lo, monkeypatch, *, enabled=(0, 1), pfc_gap=()):
    soc = {"linux_dt": {f"canfd{c}": {"channel": c, "label": "canfd"}
                        for c in (0, 1, 2)},
           "peripherals": {"can_fd": 3}}
    doc = {"assignable": {
        "e1m_can0": {"soc_instance": "canfd2", "linux_enable": 0 in enabled},
        "e1m_can1": {"soc_instance": "canfd0", "linux_enable": 1 in enabled},
        "e1m_can2": {"soc_instance": "canfd1", "linux_enable": 2 in enabled,
                     "hw_blocked": True},
    }}
    own = {k: "a55" for k in doc["assignable"]}
    monkeypatch.setattr(
        lo, "instance_pfc",
        lambda _soc, e: [("p", None if e["soc_instance"] in pfc_gap else 1)])
    return doc, soc, own


def test_can_netdev_is_the_rank_among_enabled_channels(lo, monkeypatch):
    doc, soc, own = _can_fixture(lo, monkeypatch)
    # e1m_can1 -> channel 0 -> can0; e1m_can0 -> channel 2 -> can1;
    # hw_blocked e1m_can2 never enabled -> "".
    assert lo.can_netdev_map(doc, soc, own) == ["can1", "can0"]


def test_render_disables_the_other_channels_and_emits_the_netdev_map(lo, monkeypatch):
    doc, soc, own = _can_fixture(lo, monkeypatch)
    monkeypatch.setattr(lo, "cm33_clocks", lambda *_a, **_k: [])
    for e in doc["assignable"].values():
        e["linux_evidence"] = "bench"
        if e.get("hw_blocked"):
            e["hw_blocked"] = {"reason": "test"}
    monkeypatch.setattr(
        lo, "instance_pfc",
        lambda _soc, _e: [({"pad": "P", "peripheral": "TX"}, ("P0", 1, 2))])
    text, labels = lo.render(doc, soc, {}, own)
    # channel0 + channel2 are enabled by Linux; only channel1 is disabled.
    assert '&canfd {\n\tchannel1 {\n\t\tstatus = "disabled";\n\t};\n};' in text
    assert 'channel0 {\n\t\tstatus = "disabled"' not in text
    assert 'channel2 {\n\t\tstatus = "disabled"' not in text
    assert '/ {\n\talp,e1m-can-netdev = "can1", "can0";\n};' in text
    assert "canfd/channel1" in labels


def test_can_netdev_blank_for_a_bus_linux_does_not_enable(lo, monkeypatch):
    doc, soc, own = _can_fixture(lo, monkeypatch, enabled=(1,))
    assert lo.can_netdev_map(doc, soc, own) == ["", "can0"]


def test_can_netdev_blank_when_a_pad_has_no_pfc_code(lo, monkeypatch):
    doc, soc, own = _can_fixture(lo, monkeypatch, pfc_gap=("canfd2",))
    assert lo.can_netdev_map(doc, soc, own) == ["", "can0"]


def test_can_netdev_empty_without_a55_can(lo, monkeypatch):
    doc, soc, own = _can_fixture(lo, monkeypatch)
    assert lo.can_netdev_map(doc, soc, {k: "m33" for k in own}) == []


def test_wired_pads_render_inside_the_som_power_node(zb):
    pads = [{"signal": "OSPI1_INTn", "silicon_pad": "P15_2"},
            {"signal": "OSPI0_RESETn", "silicon_pad": "P15_6"}]
    text = "\n".join(zb._aen_som_power_dts({}, {}, {"brd_i2c": {}}, pads))
    assert "alp,wired-lpgpio-pads = <2 6>;" in text
    assert "OSPI1_INTn P15_2, OSPI0_RESETn P15_6." in text
    assert text.index('pinctrl-names = "default";') < text.index("alp,wired-lpgpio-pads")


def test_no_wired_pads_emits_no_property(zb):
    text = "\n".join(zb._aen_som_power_dts({}, {}, {"brd_i2c": {}}, []))
    assert "alp,wired-lpgpio-pads" not in text


def test_loader_reads_and_validates_the_wired_pads(zb, tmp_path):
    d = tmp_path / "e1m_modules" / "aen"
    d.mkdir(parents=True)
    f = d / "on-module-links.yaml"
    f.write_text("wired_lpgpio_pads:\n"
                 "  - {signal: A, silicon_pad: P15_3}\n")
    assert zb._load_aen_wired_lpgpio_pads(tmp_path) == [
        {"signal": "A", "silicon_pad": "P15_3"}]
    f.write_text("wired_lpgpio_pads:\n"
                 "  - {signal: B, silicon_pad: P14_3}\n")
    with pytest.raises(zb.ZephyrBoardEmitError, match="not an LPGPIO pad"):
        zb._load_aen_wired_lpgpio_pads(tmp_path)
    f.write_text("power_domains: {}\n")
    assert zb._load_aen_wired_lpgpio_pads(tmp_path) == []
