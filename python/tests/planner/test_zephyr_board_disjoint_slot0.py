# SPDX-License-Identifier: Apache-2.0
"""A dual-M55 AEN SoM that declares no per-role `<role>_slot0` region is
REFUSED, not silently given the stock symmetric layout (tan-cli#756,
hand-ported from alp-sdk#1446's `_aen_require_disjoint_slot0`).

`_aen_role_slot0_map` answers None when NO role declares a slot0 window,
and the caller then falls back to `_AEN_MRAM_BASE + _AEN_MCUBOOT_KIB * 1024`
-- one address, used for BOTH roles. On a SoM with two M55 cores that is
alp-sdk#1069's defect: `m55_he` and `m55_hp` boot from the same physical App
MRAM, so flashing one core writes over the other's slot0 window, with no
diagnostic on either side. Five shipping SoMs (E1M-AEN301, E1M-AEN401,
E1M-AEN501, E1M-AEN601, E1M-AEN701) carried exactly that layout until
alp-sdk#1445 gave them explicit maps.

The HALF-authored case -- one role declares a window, its sibling does not
-- already raised inside `_aen_role_slot0_map`. This module covers the
FULLY unauthored case, which was silent.

It fires on nothing in the current corpus, by design: after alp-sdk#1445
every AEN SoM declares disjoint windows. Its value is the NEXT dual-M55 AEN
SoM, refused at authoring time instead of discovered by someone flashing one
core on a bench and losing the other's slot0. That is also why the negative
cases below are not optional -- a guard that refused everything would pass
the first test and break every real SKU.

Hermetic: `_aen_require_disjoint_slot0` reads only its own arguments, never
the bound checkout. `tan.planner` still needs SOME bound root at import time
(`tan/planner_root.py`), hence the same `ALP_SDK_ROOT`-gated skip every
other `tests/planner/` module carries.
"""

from __future__ import annotations

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401 -- `_bound_sdk` imported for its side effect (fixture registration)

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.zephyr_board requires SOME "
           "bound root (tan/planner_root.py), even though the assertions "
           "below read only their own arguments. A SKIP about the missing "
           "root, not a pass.",
)


def _mod():
    """Imported inside each call so the module imports after
    `bind_sdk_root` has run -- same reason
    `test_zephyr_board_aen_log_mode_default.py::_mod` does it lazily."""
    import tan.planner.zephyr_board as m

    return m


#: Both M55 roles present -- the shape that can collide.
_DUAL_M55 = {"topology": {"m55_he": {}, "m55_hp": {}}}

#: Real E1M-AEN801 region names (`metadata/e1m_modules/E1M-AEN801.yaml`);
#: only `name:` is read here, so the bases are deliberately omitted rather
#: than invented.
_DISJOINT_MAP = [{"name": "he_slot0"}, {"name": "hp_slot0"}]


def test_a_dual_m55_som_with_no_slot0_region_is_refused():
    """The regression itself. Without `_aen_require_disjoint_slot0` this
    returns quietly and both roles resolve one shared slot0 address."""
    zb = _mod()
    with pytest.raises(zb.ZephyrBoardEmitError) as excinfo:
        zb._aen_require_disjoint_slot0("E1M-AEN301", _DUAL_M55, None)
    message = str(excinfo.value)
    # The SKU and both core ids, so the message names the board to fix and
    # the pair that would have collided -- not just "invalid preset".
    assert "E1M-AEN301" in message
    assert "m55_he" in message and "m55_hp" in message
    assert "slot0" in message
    # The remedy, by path: this is authored in the SoM preset, not in tan.
    assert "memory_map" in message
    assert "E1M-AEN801" in message, (
        f"the refusal should name the preset whose shape to copy: {message}")


def test_an_empty_memory_map_is_the_same_refusal_as_no_memory_map():
    """`memory_map: []` and an absent `memory_map:` are the same fact --
    no role declares a window -- so both must refuse. A guard written as
    `if memory_map is not None` would let the empty list through into the
    symmetric fallback."""
    zb = _mod()
    with pytest.raises(zb.ZephyrBoardEmitError):
        zb._aen_require_disjoint_slot0("E1M-AEN301", _DUAL_M55, [])


def test_a_dual_m55_som_that_declares_disjoint_windows_is_accepted():
    """First negative control: the guard must not refuse E1M-AEN801, the
    SKU whose layout its own message holds up as correct."""
    zb = _mod()
    zb._aen_require_disjoint_slot0("E1M-AEN801", _DUAL_M55, _DISJOINT_MAP)


def test_one_declared_window_is_left_to_the_resolvers_own_refusal():
    """A map declaring only `hp_slot0` passes HERE deliberately: the
    half-authored case is `_aen_role_slot0_map`'s to refuse, with a message
    naming the specific missing role. Duplicating that refusal in this
    guard would replace a precise diagnostic with a vaguer one."""
    zb = _mod()
    zb._aen_require_disjoint_slot0(
        "E1M-AEN801", _DUAL_M55, [{"name": "hp_slot0"}])


@pytest.mark.parametrize("topology", [
    {"m55_hp": {}},                      # single M55
    {"m55_he": {}},                      # single M55, other role
    {"a32_cluster": {}, "m55_hp": {}},   # one M55 alongside a non-M55 core
    {},                                  # no cores at all
])
def test_a_som_without_two_m55_cores_is_never_refused(topology):
    """Second negative control, and the reason the guard counts cores
    rather than just looking for the region: with no sibling M55 there is
    nothing the symmetric layout can clobber, so that layout stays correct
    and refusing it would break every single-M55 AEN SKU."""
    zb = _mod()
    zb._aen_require_disjoint_slot0("E1M-AEN101", {"topology": topology}, None)


def test_a_region_named_something_else_does_not_count_as_a_slot0_window():
    """The `_slot0` suffix is load-bearing: an unrelated `memory_map:` (an
    rpmsg carve-out, say) declares regions but no `<role>_slot0`, and that
    is still the fully-unauthored state -- `_aen_role_slot0_map` falls
    through to the symmetric layout for it."""
    zb = _mod()
    with pytest.raises(zb.ZephyrBoardEmitError):
        zb._aen_require_disjoint_slot0(
            "E1M-AEN301", _DUAL_M55,
            [{"name": "rpmsg_shm"}, {"name": "mcuboot"}])
