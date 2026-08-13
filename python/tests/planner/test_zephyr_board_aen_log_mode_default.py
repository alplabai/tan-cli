# SPDX-License-Identifier: Apache-2.0
"""Every AEN board's generated `Kconfig.defconfig` defaults `LOG_MODE` to
`LOG_MODE_MINIMAL` (tan-cli#690, hand-ported from alp-sdk#1407).

Zephyr's inherited `choice LOG_MODE` default is `LOG_MODE_DEFERRED`, which
hands every log record -- and, via `CONFIG_LOG_PRINTK`, the Alp SDK boot
banner with it -- to the log processing thread. That thread runs BELOW
`main`'s `CONFIG_MAIN_THREAD_PRIORITY=0` and time-slicing only rotates
threads of EQUAL priority, so it is starved outright by the non-yielding
busy-loop `main()` the AEN bench procedure requires (an idling M55 makes the
Secure Enclave gate the DAP and the SE-UART together). Measured on
E1M-AEN801 silicon (alp-sdk issue #1373): zero UART bytes out of a running,
fault-free board. `LOG_MODE_MINIMAL` formats and writes in the calling
context, so it cannot be starved the same way.

Reproduced without this fix: reverting the `_AEN_LOG_MODE_DEFAULT` block
and its `_aen_kconfig_defconfig` call site drops
`test_tan_generate_writes_a_zephyr_board_tree_byte_for_byte`
(`tests/parity/test_planner_emit_parity.py`) to a `Kconfig.defconfig`
byte-mismatch against alp-sdk's own `gen_zephyr_board.py`
(`ALP_SDK_ROOT`-gated). This module covers the same invariant hermetically,
with no bound SDK checkout required for the assertions themselves --
`_aen_kconfig_defconfig`/`_aen_defconfig` read no metadata, only their own
arguments. `tan.planner` still requires SOME bound root at import time
(`tan/planner_root.py`), which is why this module carries the same
`ALP_SDK_ROOT`-gated skip every other `tests/planner/` module does.
"""

from __future__ import annotations

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.zephyr_board requires SOME "
           "bound root (tan/planner_root.py), even though the assertions "
           "below read only their own arguments, never the bound "
           "checkout's content. A SKIP about the missing root, not a pass.",
)


@pytest.fixture(autouse=True)
def _bound_sdk():
    bind_sdk_root(SDK)
    yield


def _mod():
    """Imported inside each call so the module imports before
    `bind_sdk_root` has run (collection order), the same reason
    `test_zephyr_board_metadata_facts.py`'s `_emit` helper does it lazily."""
    import tan.planner.zephyr_board as m

    return m


def test_every_aen_kconfig_defconfig_defaults_log_mode_minimal():
    zb = _mod()
    for dir_name, role, part in (
        ("alp_e1m_aen801_m55_hp", "hp", "E8"),
        ("alp_e1m_aen801_m55_he", "he", "E8"),
        ("alp_e1m_aen401_m55_hp", "hp", "E4"),
    ):
        text = zb._aen_kconfig_defconfig(dir_name, role, part)
        assert (
            "choice LOG_MODE\n\tdefault LOG_MODE_MINIMAL\nendchoice\n" in text
        ), (
            f"{dir_name}'s generated Kconfig.defconfig lost its "
            f"LOG_MODE_MINIMAL board default -- a busy-loop main() on this "
            f"board will print nothing (alp-sdk#1373)")
        # The LOG_MODE default must stay INSIDE the board's `if BOARD_<sym>`
        # guard, same as ROM_START_OFFSET -- a board-scoped default outside
        # it would leak onto every other board sharing this Kconfig tree.
        board_sym = dir_name.upper()
        assert text.rstrip().endswith(f"endif # BOARD_{board_sym}")
        log_mode_pos = text.index("choice LOG_MODE")
        endif_pos = text.rindex(f"endif # BOARD_{board_sym}")
        assert log_mode_pos < endif_pos


def test_the_defconfig_never_assigns_the_choice_symbol_directly():
    """`CONFIG_LOG_MODE_MINIMAL=y` in the board `_defconfig` reaches the same
    end state when `CONFIG_LOG=y`, but also assigns an INVISIBLE choice
    symbol on every `CONFIG_LOG=n` fragment (47 of them under
    `examples/aen/` on the alp-sdk side) -- Zephyr's own
    `check_assigned_choice_values()` then warns on each one. The
    `Kconfig.defconfig` `choice`/`default` form used above is inert there;
    `_aen_defconfig` (the `_defconfig` emitter) must stay free of the direct
    assignment."""
    zb = _mod()
    row = {"silicon_pad": "P3_4"}
    text = zb._aen_defconfig("uart0", row, row, slot0_base=0x80080000)
    assert "CONFIG_LOG_MODE_MINIMAL=y" not in text
