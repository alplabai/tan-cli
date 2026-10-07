# SPDX-License-Identifier: Apache-2.0
"""Per-product core ownership (alp-sdk#2673 / #2674): assignable defaults,
board.yaml override, rejection of bad cores / instances, fixed rows untouched,
the system-manifest `ownership:` key, the CM33 overlay and the Linux fragment.

Ported from alp-sdk's `tests/scripts/test_core_ownership.py` for the
relocated `tan.planner.ownership` / `tan.planner.linux_ownership` modules.
The two upstream cases that drive `gen_zephyr_board.emit_zephyr_board` (the
board tree declaring an assignable node `disabled`) belong with the
`tan.planner.zephyr_board` hand-port, not here.

Real-SDK-gated: reads the bound checkout's `metadata/`, its
`examples/multicore/rpmsg-*` projects and the committed
`e1m-v2n-ownership.dtsi`.
"""
from __future__ import annotations

import shutil

import pytest
import yaml

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these read the bound metadata/ tree and examples. "
           "A SKIP about the missing root, not a pass.",
)


def _err():
    from tan.planner.models import OrchestratorError

    return OrchestratorError


@pytest.fixture
def doc():
    from tan.planner.ownership import load_ownership_doc

    return load_ownership_doc(SDK / "metadata", "v2n")


def _src():
    return SDK / "examples" / "multicore" / "rpmsg-v2n"


def _project(tmp_path, ownership=None):
    dst = tmp_path / "p"
    shutil.copytree(_src(), dst)
    b = dst / "board.yaml"
    if ownership is not None:
        b.write_text(b.read_text(encoding="utf-8") + "\n" + yaml.safe_dump({"ownership": ownership}),
                     encoding="utf-8")
    return b


def _load(path, **kw):
    from tan.planner import load_board_yaml

    return load_board_yaml(path, **kw)


def _manifest(project):
    from tan.planner import emit_system_manifest

    return yaml.safe_load(emit_system_manifest(project))


def _resolve(*args, **kw):
    from tan.planner.ownership import resolve_ownership

    return resolve_ownership(*args, **kw)


def test_defaults_are_a55_for_every_assignable_instance(doc):
    got = _resolve(doc)
    assert set(got) == {"e1m_uart0", "e1m_uart1", "e1m_spi0", "e1m_can0", "e1m_can1"}
    assert set(got.values()) == {"a55"}


def test_restating_the_default_is_accepted_and_fixed_rows_unaffected(doc):
    before = list(doc["core_ownership"])
    got = _resolve(doc, {"e1m_spi0": "a55"})
    assert got["e1m_spi0"] == "a55" and got["e1m_uart0"] == "a55"
    assert doc["core_ownership"] == before
    fixed = {r["pad"] for r in doc["core_ownership"]}
    assert not fixed & {"P50", "P51", "P52", "P53", "P84", "P85", "P86", "P87",
                        "P90", "P91", "P92", "P93", "P94"}


def _doc(**entry):
    e = {"default": "a55", "candidates": ["a55", "m33"], "rows": []}
    e.update(entry)
    return {"assignable": {"e1m_x": e}}


def test_override_needs_an_m33_candidate_and_block_so_both_trees_can_follow():
    with pytest.raises(_err(), match="e1m_x: 'm33' differs from the SoM default 'a55'.*no `m33:`"):
        _resolve(_doc(), {"e1m_x": "m33"})
    assert _resolve(_doc(), {"e1m_x": "a55"}) == {"e1m_x": "a55"}
    blk = {"m33": {"dt_label": "sci0", "alias": "alp-uart9"}}
    assert _resolve(_doc(**blk), {"e1m_x": "m33"}) == {"e1m_x": "m33"}


def test_handing_a_node_to_linux_needs_linux_enable():
    blk = {"default": "m33", "m33": {"dt_label": "sci0", "alias": "alp-uart9"}}
    with pytest.raises(_err()) as exc:
        _resolve(_doc(**blk), {"e1m_x": "a55"})
    assert str(exc.value) == (
        "board.yaml ownership: e1m_x: Linux enablement is not bench-evidenced "
        "(`linux_enable` unset in core-ownership.yaml); it cannot be handed to 'a55'")
    assert _resolve(_doc(linux_enable=True, **blk), {"e1m_x": "a55"}) == {"e1m_x": "a55"}


def test_hw_blocked_instance_rejects_an_override_with_the_reason(doc):
    blocked = _doc(hw_blocked={"reason": "P90-P92 not 3.3 V tolerant"})
    with pytest.raises(_err()) as exc:
        _resolve(blocked, {"e1m_x": "m33"})
    assert str(exc.value) == (
        "board.yaml ownership: e1m_x is hardware-blocked on every core: "
        "P90-P92 not 3.3 V tolerant")
    assert doc["assignable"]["e1m_spi0"]["candidates"] == ["a55"]


def test_override_to_a_core_the_project_does_not_declare_is_rejected():
    with pytest.raises(_err()) as exc:
        _resolve(_doc(), {"e1m_x": "a55"}, declared_core_types={"cortex-m33"})
    assert str(exc.value) == (
        "board.yaml ownership: e1m_x is assigned to 'a55' but board.yaml "
        "`cores:` does not declare a cortex-a55 core")


@pytest.mark.parametrize("inst", ["e1m_can0", "e1m_uart0", "e1m_uart1", "e1m_spi0"])
def test_invalid_core_rejected_naming_instance_and_allowed(doc, inst):
    with pytest.raises(_err()) as exc:
        _resolve(doc, {inst: "m33"})
    assert str(exc.value) == (
        f"board.yaml ownership: {inst} cannot be owned by 'm33'; allowed cores: ['a55']")


def test_unknown_instance_rejected(doc):
    with pytest.raises(_err(), match="board.yaml ownership: unknown instance 'e1m_i2c0'; "
                                     "assignable instances: "):
        _resolve(doc, {"e1m_i2c0": "a55"})


def test_fixed_pad_instance_cannot_be_overridden(doc):
    with pytest.raises(_err(), match="unknown instance 'GD32_SPI.MOSI'"):
        _resolve(doc, {"GD32_SPI.MOSI": "a55"})


def test_loader_and_manifest_roundtrip(tmp_path):
    out = _manifest(_load(_project(tmp_path)))
    assert out["ownership"]["e1m_spi0"] == "a55"
    out = _manifest(_load(_project(tmp_path / "o", {"e1m_spi0": "a55"})))
    assert out["ownership"]["e1m_spi0"] == "a55"


def test_loader_rejects_bad_override(tmp_path):
    with pytest.raises(_err(), match="e1m_can0 cannot be owned by 'm33'"):
        _load(_project(tmp_path, {"e1m_can0": "m33"}))


def test_loader_rejects_override_to_m33_on_a_default_a55_instance(tmp_path):
    with pytest.raises(_err(), match="e1m_spi0 cannot be owned by 'm33'"):
        _load(_project(tmp_path, {"e1m_spi0": "m33"}))


def test_non_v2n_sku_has_no_ownership_key():
    out = _manifest(_load(SDK / "examples/multicore/rpmsg-aen/board.yaml"))
    assert "ownership" not in out


def test_validate_assignable_catches_bad_metadata():
    from tan.planner.ownership import validate_assignable

    bad = {"core_ownership": [{"peripheral": "X", "pad": "P1"}],
           "assignable": {"i": {"default": "m33", "candidates": ["a55", "zz"],
                                "rows": [{"peripheral": "X", "pad": "P1"}]}}}
    msgs = "\n".join(validate_assignable(bad, set(), {"cortex-a55"}))
    assert "default 'm33' not in candidates" in msgs
    assert "'zz' is not a core" in msgs
    assert "matches no owner=renesas row" in msgs
    assert "also a FIXED" in msgs


def _meta_with_m33_uart0(tmp_path):
    """A metadata tree whose e1m_uart0 DEFAULTS to the M33 (the real one is
    a55-only).  Its PFC functions come from the real SoC linux_dt.UART0.pinmux."""
    root = tmp_path / "metadata"
    shutil.copytree(SDK / "metadata", root)
    f = root / "e1m_modules" / "v2n" / "core-ownership.yaml"
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    e = d["assignable"]["e1m_uart0"]
    e["candidates"] = ["a55", "m33"]
    e["default"] = "m33"
    e["m33"] = {"dt_label": "sci0", "alias": "alp-uart9", "kconfig": ["CONFIG_SERIAL=y"],
                "pinctrl": {"group_label": "sci0_asg_pins", "node": "sci0_asg",
                            "child_node": "sci0-asg-pinmux"}}
    f.write_text(yaml.safe_dump(d), encoding="utf-8")
    return root


def test_project_overlay_and_conf_enable_only_the_owner(tmp_path):
    from tan.planner import _slice_alp_conf
    from tan.planner.ownership import project_m33_overlay

    root = _meta_with_m33_uart0(tmp_path)
    proj = _load(_project(tmp_path), metadata_root=root)
    dts, kc = project_m33_overlay(proj, "m33_sm")
    dts = " ".join(dts)
    assert "&sci0 {" in dts and "alp-uart9 = &sci0;" in dts and kc == ["CONFIG_SERIAL=y"]
    conf = _slice_alp_conf(proj, proj.cores["m33_sm"]).splitlines()
    i = conf.index("# Assignable peripherals owned by this core (board.yaml `ownership:`).")
    assert conf[i + 1] == "CONFIG_SERIAL=y"
    assert project_m33_overlay(proj, "a55_cluster") == ([], [])
    plain = _load(_project(tmp_path / "d"))
    assert project_m33_overlay(plain, "m33_sm") == ([], [])


def test_m33_assignment_without_devicetree_block_is_a_clear_error():
    from tan.planner.ownership import m33_overlay

    d = {"assignable": {"e1m_x": {"default": "a55", "candidates": ["a55", "m33"]}}}
    with pytest.raises(_err(), match="e1m_x is assigned to 'm33' but core-ownership.yaml "
                                     "carries no `m33:` devicetree block"):
        m33_overlay(d, {"e1m_x": "m33"})


def test_pad_pfc_rejects_letter_ports():
    from tan.planner.ownership import pad_pfc

    with pytest.raises(_err(), match="letter ports are not mapped"):
        pad_pfc("PA7", 1)


def test_validate_assignable_soc_instance_and_m33_pfc():
    from tan.planner.ownership import pad_pfc, validate_assignable

    pairs = {("X", "P50")}
    d = {"assignable": {"i": {"soc_instance": "UART0", "default": "a55", "candidates": ["a55"],
         "rows": [{"peripheral": "X", "pad": "P50"}]}}}
    ok = {"UART0": {"label": "sci0", "pinmux": {"X": 1}}}
    assert validate_assignable(d, pairs, {"cortex-a55"}, ok) == []
    assert "not a key of the SoC linux_dt" in "\n".join(
        validate_assignable(d, pairs, {"cortex-a55"}, {}))
    d["assignable"]["i"]["m33"] = {}
    assert "no function for ['X']" in "\n".join(
        validate_assignable(d, pairs, {"cortex-a55"}, {"UART0": {"label": "sci0"}}))
    assert pad_pfc("P50", 1) == ("PORT_05", 0, 1) and pad_pfc("P96", 2) == ("PORT_09", 6, 2)


def test_hw_blocked_instance_cannot_be_enabled_on_m33(doc):
    from tan.planner.ownership import m33_overlay

    d = {"assignable": {"e1m_spi0": {"hw_blocked": {"reason": "P90-P92 not 3.3 V tolerant"},
                                     "m33": {"dt_label": "rspi0", "alias": "alp-spi2"}}}}
    with pytest.raises(_err(), match="hardware-blocked.*3.3 V"):
        m33_overlay(d, {"e1m_spi0": "m33"})
    assert doc["assignable"]["e1m_spi0"]["hw_blocked"]["reason"]


def test_override_flips_cm33_overlay_and_linux_fragment_consistently(tmp_path):
    """One board.yaml override drives the CM33 node, the Linux node status and
    the CM33 clock hold; the default project changes none of them."""
    from tan.planner.linux_ownership import emit_linux_ownership_dts
    from tan.planner.ownership import project_m33_overlay

    root = _meta_with_m33_uart0(tmp_path)
    f = root / "e1m_modules" / "v2n" / "core-ownership.yaml"
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    d["assignable"]["e1m_uart0"]["default"] = "a55"
    f.write_text(yaml.safe_dump(d), encoding="utf-8")

    plain = _load(_project(tmp_path / "d"), metadata_root=root)
    own = _load(_project(tmp_path / "o", {"e1m_uart0": "m33"}), metadata_root=root)
    assert own.ownership["e1m_uart0"] == "m33" and plain.ownership["e1m_uart0"] == "a55"

    assert project_m33_overlay(plain, "m33_sm") == ([], [])
    assert "&sci0 {" in " ".join(project_m33_overlay(own, "m33_sm")[0])
    lin_own, lin_plain = emit_linux_ownership_dts(own), emit_linux_ownership_dts(plain)
    assert '&sci0 {\n\tstatus = "disabled";' in lin_own
    assert '&sci0 {\n\tstatus = "disabled";' not in lin_plain
    clk = own.soc_spec["linux_dt"]["UART0"]["cpg_clocks"]
    assert all(f'"{c}"' in lin_own for c in clk)
    assert not any(f'"{c}"' in lin_plain for c in clk)


def test_default_project_fragment_equals_the_committed_som_default_fragment():
    from tan.planner.linux_ownership import emit_linux_ownership_dts

    proj = _load(_src() / "board.yaml")
    committed = SDK / "meta-alp-sdk/recipes-kernel/linux/linux-renesas/e1m-v2n-ownership.dtsi"
    assert emit_linux_ownership_dts(proj) == committed.read_text(encoding="utf-8")


def test_non_assignable_som_emits_a_stub():
    from tan.planner.linux_ownership import emit_linux_ownership_dts

    out = emit_linux_ownership_dts(_load(SDK / "examples/multicore/rpmsg-aen/board.yaml"))
    assert out == "/* No assignable core ownership for this SoM family; nothing to emit. */\n"


def test_ownership_doc_is_read_from_the_given_metadata_root(tmp_path, doc):
    """A metadata root not named `metadata` resolves under the root itself."""
    from tan.planner.ownership import load_ownership_doc, ownership_doc_rel

    root = tmp_path / "meta-override"
    dst = root / "e1m_modules" / "v2n"
    dst.mkdir(parents=True)
    shutil.copy(SDK / "metadata/e1m_modules/v2n/core-ownership.yaml", dst)
    assert load_ownership_doc(root, "v2n") == doc
    assert ownership_doc_rel(root, "v2n") == "metadata/e1m_modules/v2n/core-ownership.yaml"
