# SPDX-License-Identifier: Apache-2.0
"""`slugs._CHIP_SUBSYSTEMS` is a HAND-PORT, and it went stale (tan-cli#868).

The chip -> Zephyr-subsystem table lives upstream in alp-sdk's
`scripts/alp_project_emit/__init__.py` and was hand-ported into
`tan/planner/slugs.py` (its one and only consumer, `kconfig._emit_chips`).
Hand-ports are pinned by `HAND_PORT_HASHES`, which hashes the UPSTREAM FILE at
`HAND_PORT_PINNED_SDK_COMMIT` -- deliberately an OLDER commit than
`PINNED_SDK_COMMIT`. That pin therefore stayed green while alp-sdk#1487 added
ten SoM-intrinsic chips plus `gd32g553` upstream and tan's copy did not move.

The consequence was not cosmetic. `gd32g553` maps to `("SPI", "I2C")` because
its driver declares `depends on (SPI || I2C)`, so on EVERY GD32-bearing SoM
(the whole E1M-X V2N/V2M family) alp-sdk emitted `CONFIG_SPI=y` into the
per-core Kconfig fragment and tan did not -- a Zephyr `depends on` left unmet,
which Kconfig drops silently. That single missing line is what the dispatched
parity suite reported as `--emit build-plan differs -- line 24` across 57
boards in tan-cli#868.

Two assertions, deliberately different in kind:

  * the MEMBERSHIP assertions pin what tan-cli#868 actually ported, and hold
    regardless of what alp-sdk does next.
  * the EQUALITY assertion re-reads the upstream literal out of the bound
    checkout, so the NEXT upstream chip addition fails here by name instead of
    surfacing 57 boards later as a byte-diff in a build-plan blob. It is the
    same shape as the freshness gate, applied to the one hand-ported table
    whose upstream pin is allowed to lag.

Both need a bound root: the membership half for the import, the equality half
for the checkout it reads. Without one they SKIP, naming the variable.
"""
from __future__ import annotations

import ast
import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.slugs requires SOME bound "
           "root, and the equality test below reads the upstream table out "
           "of that checkout. A SKIP about the missing root, not a pass.",
)

_UPSTREAM_REL = "scripts/alp_project_emit/__init__.py"

# alp-sdk#1487's additions, the delta tan-cli#868 ported. `gd32g553` is the
# load-bearing one -- see this module's docstring.
_SOM_INTRINSIC = {
    "act8760": ("I2C",),
    "tps628640": ("I2C",),
    "pi3dbs12212": ("GPIO",),
    "pca9451a": ("I2C",),
    "da9292": ("I2C",),
    "clk_5l35023b": ("I2C",),
    "murata_lbee5hy2fy": ("GPIO",),
    "deepx_dxm1": ("GPIO",),
    "gd32_swd": ("GPIO",),
    "gd32g553": ("SPI", "I2C"),
}


def _upstream_table() -> dict[str, tuple[str, ...]]:
    """`_CHIP_SUBSYSTEMS` as alp-sdk declares it, read as a literal.

    Parsed with `ast`, never imported: `alp_project_emit` pulls in the SDK's
    own Python package graph, which is exactly the dependency the relocation
    removed (ADR-0017). Reading the literal keeps this test from re-creating
    it just to compare a dict.
    """
    source = (SDK / _UPSTREAM_REL).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        targets = getattr(node, "targets", None) or (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_CHIP_SUBSYSTEMS":
                return ast.literal_eval(node.value)
    raise AssertionError(
        f"_CHIP_SUBSYSTEMS is no longer a module-level literal in "
        f"{_UPSTREAM_REL} of the bound checkout -- the hand-port's source "
        f"moved, so this comparison needs re-aiming, not deleting")


def test_the_som_intrinsic_chips_carry_their_subsystem_dependency():
    """alp-sdk#1487: each of these has a `depends on` line in
    zephyr/kconfigs/chips.kconfig, so enabling the chip without its subsystem
    means Kconfig drops the chip assignment as unmet."""
    from tan.planner.slugs import _CHIP_SUBSYSTEMS

    missing = {k: v for k, v in _SOM_INTRINSIC.items()
               if _CHIP_SUBSYSTEMS.get(k) != v}
    assert not missing, (
        "chip -> subsystem entries missing or wrong in tan/planner/slugs.py: "
        f"{missing}")


def test_the_gd32_bridge_turns_on_both_of_its_hybrid_transports():
    """The GD32 bridge protocol is genuinely hybrid -- both transports are
    compiled in and either is usable at runtime -- so the driver's
    `depends on (SPI || I2C)` is satisfied by turning BOTH on. Picking one
    would silently drop the other, which is alp-sdk#1487's own bug class."""
    from tan.planner.slugs import _CHIP_SUBSYSTEMS

    assert _CHIP_SUBSYSTEMS["gd32g553"] == ("SPI", "I2C")


def test_the_hand_ported_table_matches_the_bound_alp_sdk_checkout():
    """The drift catcher. When it fails, alp-sdk changed the table and tan's
    copy has not been re-audited -- port the named entries into
    `tan/planner/slugs.py`, do not weaken this test."""
    from tan.planner.slugs import _CHIP_SUBSYSTEMS

    upstream = _upstream_table()
    only_upstream = {k: v for k, v in upstream.items()
                     if _CHIP_SUBSYSTEMS.get(k) != v}
    only_tan = {k: v for k, v in _CHIP_SUBSYSTEMS.items()
                if k not in upstream}
    assert not only_upstream and not only_tan, (
        "tan/planner/slugs.py::_CHIP_SUBSYSTEMS has drifted from the bound "
        f"alp-sdk checkout's {_UPSTREAM_REL}.\n"
        f"  missing or wrong in tan: {only_upstream}\n"
        f"  present only in tan:     {only_tan}")
