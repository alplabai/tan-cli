# SPDX-License-Identifier: Apache-2.0
"""tan-cli#654: `tan build` emitted `CONFIG_ALP_SDK_CHIP_DP83825=y` for a chip
with no driver, aborting every AEN Zephyr slice at Kconfig ("attempt to
assign the value 'y' to the undefined symbol ALP_SDK_CHIP_DP83825").

alp-sdk fixed the same defect in its own emitter as alp-sdk#1241
(`_chip_has_driver`, ported here verbatim by the tan-cli#582/#593 planner
re-syncs): a `CONFIG_ALP_SDK_CHIP_<X>` line is only meaningful when the
symbol exists, and the symbol only exists when `chips/<x>/` does -- every
declaration in `zephyr/kconfigs/chips.kconfig` reads "Compile
chips/<part>/<part>.c". `_slugs_from_on_module` already applied this rule
per-FIELD (`ospi_memories`, `hyperram` are excluded because those parts have
no driver); it has to hold per-CHIP too, because a scalar `on_module:` field
can name a real chip manifest with nothing behind it -- which is exactly
what the AEN family's `ethernet_phy: dp83825` did (`driver_status: none`, no
`chips/dp83825/` directory).

This module is the dedicated, GENERIC regression alp-sdk#1380 asked for: it
does not hardcode "dp83825" as the only chip the rule must reject (that
would leave the next undriven chip free to repeat #320 / #485 / this one) --
`test_the_rule_rejects_every_undriven_chip_generically` sweeps every chip
manifest under the bound SDK's `metadata/chips/` and asserts the rule holds
for whichever of them currently lack a driver, whatever their name.

Importing `tan.planner.*` needs a bound alp-sdk root (same requirement as
`tests/core/test_planner_relocation_fixes.py`, whose `planner`/`_sdk_root`
shape this file reuses); `tests.conftest.sdk_root()` resolves it the same
way `tests/parity/test_planner_emit_parity.py` does. Skipped, loudly,
without one -- a green run that checked nothing would be worse than a red
one.
"""

from __future__ import annotations

import pytest
import yaml

from tests.conftest import sdk_root

SDK = sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a "
    "root and import (same requirement as the parity suite)"
)


@pytest.fixture(scope="module")
def planner():
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    import tan.planner as planner_pkg

    return planner_pkg


def _chip_ids() -> list[str]:
    if SDK is None:
        return []
    ids = []
    for f in sorted((SDK / "metadata" / "chips").glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        cid = doc.get("chip_id")
        if cid:
            ids.append(str(cid))
    return ids


def test_there_are_chips_to_check() -> None:
    """Guard against the guard: an empty glob would make the sweep below
    pass while covering nothing."""
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    assert len(_chip_ids()) >= 50, "chip-manifest glob has drifted"


def test_dp83825_has_no_driver_and_so_gets_no_kconfig_symbol(planner) -> None:
    """The concrete regression, at the `_chip_has_driver` layer. Verified to
    fail against the unfixed shape (the filter line commented out in
    `kconfig.py::_emit_chips`) before this fix landed -- see the PR body for
    the captured red/green pair; this assertion is the hermetic half."""
    from tan.planner.kconfig import _chip_has_driver

    assert not (SDK / "chips" / "dp83825").is_dir(), (
        "chips/dp83825/ now exists in the bound SDK -- if a real driver "
        "landed, this concrete case is stale and should be dropped"
    )
    assert _chip_has_driver("dp83825") is False


def test_an_aen_project_emits_no_dp83825_kconfig_line(planner) -> None:
    """End to end through `_emit_chips`, on a real AEN `board.yaml` whose
    `on_module:` names `ethernet_phy: dp83825` -- the exact shape that
    aborted every AEN Zephyr slice in tan-cli#654. Fails against the
    unfixed `_emit_chips` with
    `CONFIG_ALP_SDK_CHIP_DP83825=y` present in the emitted lines."""
    from tan.planner import kconfig

    board = SDK / "examples" / "aen" / "aen-eeprom-manifest" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not in this checkout")
    project = planner.load_board_yaml(board)

    om = project.som_preset.get("on_module") or {}
    assert om.get("ethernet_phy") == "dp83825", (
        "fixture board's SoM preset no longer carries ethernet_phy: dp83825 "
        "-- this test needs a different AEN example to stay meaningful"
    )

    lines, _subsystems, _resolved = kconfig._emit_chips(project, {})
    text = "\n".join(lines)
    assert "CONFIG_ALP_SDK_CHIP_DP83825" not in text
    # `dp83825` is still real, populated hardware -- `_resolve_chip_states`
    # (unfiltered, upstream's own design) may still carry it in the
    # resolved map; only the EMITTED Kconfig line is forbidden. Assert the
    # positive half is untouched by this fix: the genuinely driven
    # SoM-intrinsic chips for this board are still all present.
    for expected in (
        "CONFIG_ALP_SDK_CHIP_CC3501E=y",
        "CONFIG_ALP_SDK_CHIP_EEPROM_24C128=y",
        "CONFIG_ALP_SDK_CHIP_OPTIGA_TRUST_M=y",
        "CONFIG_ALP_SDK_CHIP_RV3028C7=y",
        "CONFIG_ALP_SDK_CHIP_TMP112=y",
    ):
        assert expected in text, f"{expected} missing -- fix must not remove driven chips"


@pytest.mark.parametrize("cid", _chip_ids(), ids=lambda c: c)
def test_the_rule_rejects_every_undriven_chip_generically(planner, cid: str) -> None:
    """The GENERIC direction (tan-cli#654's requirement 1): not scoped to
    "dp83825" by name. For every chip manifest the bound SDK ships, if it
    has no `chips/<cid>/` driver directory, `_chip_has_driver` must say so
    -- whichever chip that happens to be, today or after the next metadata
    field lands one that isn't `dp83825`."""
    from tan.planner.kconfig import _chip_has_driver

    has_dir = (SDK / "chips" / cid).is_dir()
    assert _chip_has_driver(cid) == has_dir, (
        f"{cid}: _chip_has_driver()={_chip_has_driver(cid)} disagrees with "
        f"the actual chips/{cid}/ directory presence ({has_dir})"
    )


def test_the_rule_is_directory_based_not_manifest_status_based(planner) -> None:
    """`_chip_has_driver`'s own documented reason for checking disk instead
    of `driver_status:` -- a status string can drift from the tree; the
    directory cannot. Pinned so a future "optimisation" that reads
    `driver_status: complete/partial/stub` instead of `chips/<x>/` cannot
    silently reintroduce the drift-prone shape alp-sdk#1241 deliberately
    avoided. Matched against the executable BODY, not the docstring (the
    docstring names `driver_status:` on purpose, to record the rejected
    alternative)."""
    import inspect

    from tan.planner.kconfig import _chip_has_driver

    body = inspect.getsource(_chip_has_driver).split('"""', 2)[-1]
    assert "is_dir()" in body
    assert "driver_status" not in body
