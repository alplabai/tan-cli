# SPDX-License-Identifier: Apache-2.0
"""A disjoint-slot0 target defaults MCUboot to single-app, and refuses an
explicit two-slot swap mode (tan-cli#696, hand-ported from alp-sdk#1413).

`emit_sysbuild_conf` defaulted `boot.swap_algorithm` unconditionally to
`scratch`. On an AEN SKU whose SoM preset declares per-role `<role>_slot0`
windows (alp-sdk#1069 -- both M55 cores share one physical App MRAM, so slot0
is split per core and the secondary/scratch slot was dropped rather than
forced to fit), the generated DT has no slot1 and no scratch partition for
any swap mode to swap into. The emitted `SB_CONFIG_MCUBOOT_MODE_SWAP_SCRATCH=y`
therefore described a boot that cannot happen, and nothing said so.

Two behaviours, both from alp-sdk#1413:

  * no `boot.swap_algorithm:` on a single-slot target -> `SINGLE_APP`, the
    boot its curated `zephyr/sysbuild/aen/sysbuild.conf` base already ships.
    Every other target keeps the historical `scratch` default.
  * an EXPLICIT `scratch`/`move`/`overwrite` on a single-slot target -> a
    loud `OrchestratorError`, because that is a `boot:` block asking for a
    partition this target's DT does not have, not a default that drifted.

Hermetic: synthetic `BoardProject`s, no bound alp-sdk checkout. The
`ALP_SDK_ROOT`-gated byte-parity suite
(`tests/parity/test_planner_emit_parity.py`) covers the same delta against
alp-sdk's own emitter; this module pins the behaviour without needing one.
"""
from __future__ import annotations

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401 -- `_bound_sdk` imported for its side effect (fixture registration)

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.secure requires SOME bound "
           "root (tan/planner_root.py), even though the assertions below "
           "read only their own arguments, never the bound checkout's "
           "content. A SKIP about the missing root, not a pass.",
)


def _mods():
    """Imported inside each call so the modules import after
    `bind_sdk_root` has run (collection order) -- the same idiom
    `test_zephyr_board_aen_log_mode_default.py` uses."""
    from tan.planner.models import BoardProject, OrchestratorError, Slice
    from tan.planner.secure import emit_sysbuild_conf

    return BoardProject, OrchestratorError, Slice, emit_sysbuild_conf

#: The E1M-AEN801 shape: one `<role>_slot0` region per M55 core, which is
#: what makes the target single-slot. Sizes are irrelevant here -- only the
#: region NAMES are read by `_aen_role_slot0_map`.
_DISJOINT_SLOT0_MEMORY_MAP = [
    {"name": "he_slot0", "accessible_from": ["m55_he"]},
    {"name": "hp_slot0", "accessible_from": ["m55_hp"]},
]


def _project(memory_map, boot, cores=("m55_hp", "m55_he")):
    BoardProject, _err, Slice, _emit = _mods()
    som_preset = {"sku": "E1M-AEN801", "family": "aen"}
    if memory_map is not None:
        som_preset["memory_map"] = memory_map
    return BoardProject(
        sku="E1M-AEN801",
        hw_rev=None,
        board_name=None,
        board_hw_rev=None,
        cores={c: Slice(core_id=c, os="zephyr", app="./src") for c in cores},
        ipc=[],
        soc_spec={},
        som_preset=som_preset,
        board_preset=None,
        boot=boot,
    )


def _swap_lines(conf: str) -> list[str]:
    return [ln for ln in conf.splitlines() if "MCUBOOT_MODE" in ln]


# --------------------------------------------------------------------------
# The default.
# --------------------------------------------------------------------------


def test_a_single_slot_target_defaults_to_single_app_not_scratch():
    _bp, _err, _sl, emit_sysbuild_conf = _mods()
    conf = emit_sysbuild_conf(_project(_DISJOINT_SLOT0_MEMORY_MAP, {"method": "mcuboot"}))
    assert _swap_lines(conf) == ["SB_CONFIG_MCUBOOT_MODE_SINGLE_APP=y"], conf


def test_a_two_slot_target_keeps_the_historical_scratch_default():
    """The control. Without it, hard-wiring SINGLE_APP passes the test above."""
    _bp, _err, _sl, emit_sysbuild_conf = _mods()
    conf = emit_sysbuild_conf(_project(None, {"method": "mcuboot"}))
    assert _swap_lines(conf) == ["SB_CONFIG_MCUBOOT_MODE_SWAP_SCRATCH=y"], conf


def test_a_memory_map_with_no_role_slot0_region_is_not_single_slot():
    """`memory_map:` is a build-policy override for any non-stock
    partitioning, not a single-slot marker. A map present for an unrelated
    reason still falls through to the stock two-slot layout, so scanning
    region names for "slot1"/"scratch" would answer wrongly here."""
    unrelated = [{"name": "rpmsg_shm", "accessible_from": ["m55_hp"]}]
    _bp, _err, _sl, emit_sysbuild_conf = _mods()
    conf = emit_sysbuild_conf(_project(unrelated, {"method": "mcuboot"}))
    assert _swap_lines(conf) == ["SB_CONFIG_MCUBOOT_MODE_SWAP_SCRATCH=y"], conf


# --------------------------------------------------------------------------
# The explicit request.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["scratch", "move", "overwrite"])
def test_an_explicit_two_slot_swap_on_a_single_slot_target_is_a_refusal(algorithm):
    _bp, OrchestratorError, _sl, emit_sysbuild_conf = _mods()
    with pytest.raises(OrchestratorError) as excinfo:
        emit_sysbuild_conf(_project(
            _DISJOINT_SLOT0_MEMORY_MAP,
            {"method": "mcuboot", "swap_algorithm": algorithm}))
    message = str(excinfo.value)
    assert algorithm in message
    assert "E1M-AEN801" in message
    # The remedy has to name the actionable edit, not just the problem.
    assert "swap_algorithm" in message


@pytest.mark.parametrize("algorithm", ["scratch", "move", "overwrite"])
def test_the_same_explicit_request_is_fine_on_a_two_slot_target(algorithm):
    """The control for the refusal: it must key on the TARGET, not on the
    algorithm name. Without this, refusing every explicit swap would pass."""
    _bp, _err, _sl, emit_sysbuild_conf = _mods()
    conf = emit_sysbuild_conf(_project(
        None, {"method": "mcuboot", "swap_algorithm": algorithm}))
    assert len(_swap_lines(conf)) == 1, conf


def test_an_explicit_none_stays_legal_on_a_single_slot_target():
    """`none` is not a two-slot mode, so the refusal must not catch it."""
    _bp, _err, _sl, emit_sysbuild_conf = _mods()
    conf = emit_sysbuild_conf(_project(
        _DISJOINT_SLOT0_MEMORY_MAP,
        {"method": "mcuboot", "swap_algorithm": "none"}))
    assert _swap_lines(conf) == ["SB_CONFIG_MCUBOOT_MODE_SINGLE_APP=y"], conf


# --------------------------------------------------------------------------
# Scope.
# --------------------------------------------------------------------------


def test_a_non_m55_project_is_never_single_slot():
    """Only `m55_he`/`m55_hp` are AEN slot0-XIP roles. A project with
    neither -- any non-AEN family -- must be untouched by this, even if it
    somehow carries a `<role>_slot0`-looking region."""
    _bp, _err, _sl, emit_sysbuild_conf = _mods()
    conf = emit_sysbuild_conf(_project(
        _DISJOINT_SLOT0_MEMORY_MAP, {"method": "mcuboot"},
        cores=("a55_cluster", "m33")))
    assert _swap_lines(conf) == ["SB_CONFIG_MCUBOOT_MODE_SWAP_SCRATCH=y"], conf
