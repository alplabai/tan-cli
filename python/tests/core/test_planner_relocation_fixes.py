# SPDX-License-Identifier: Apache-2.0
"""tan-cli#485 (`#320` recurred): three of the four defects that issue names,
each with a dedicated regression test that fails against the unfixed code.

Defect 4 (`project_loader.py`'s missing `SdkRevisionNotBuildable` gate) has
its own test in `tests/core/test_project_loader.py`, alongside the sibling
`SdkRevisionUnknown` coverage it belongs next to.

Also covers tan-cli#639 -- the SAME Defect 1 leak (`_slugs_from_on_module`
missing the `_DRIVER_STATUS_SUFFIX` filter), recurring a third time not in
`tan/planner/` itself but in the SHIPPED COMBINATION: `tan v0.5.1` (cut
2026-08-05, vendoring a pre-fix planner) against `alp-sdk v0.15.0` (cut
2026-08-04, already carrying the `nor_flash_driver_status`/
`emmc_driver_status` fields the fix guards against). The two unit tests
below `test_the_driver_status_suffix_filter_is_generic_not_enumerated`
already proved `_slugs_from_on_module` itself; what shipped broken was the
released tan binary, which no test here exercises directly -- the two new
`test_*_does_not_leak_chip_none` tests close that by rendering the real
`zephyr-conf` Kconfig fragment end to end, the same artefact `west build`
reads as `alp.conf`.

Note for whoever next mutates this file to re-verify it: `kconfig.py`'s
`_emit_chips` grew a SECOND, independent guard after tan-cli#639 shipped
(`_chip_has_driver`, alp-sdk#1241 -- a chip slug only gets a
`CONFIG_ALP_SDK_CHIP_*` line when `chips/<slug>/` actually exists on disk).
That guard alone also happens to catch `"none"` (there is no `chips/none/`),
so on THIS tree the two `test_*_does_not_leak_chip_none` tests below stay
green even with `_DRIVER_STATUS_SUFFIX` mutated out of `slugs.py` on its
own -- reproducing the ACTUAL shipped defect needs both guards removed
together (measured). That is a feature, not a gap in these tests: they
pin the customer-visible OUTCOME (no undefined symbol in the rendered
fragment) rather than one specific internal mechanism, so either guard
alone is enough to keep them passing, and losing both at once is exactly
the shape #639 was.

Importing `tan.planner.*` needs a bound alp-sdk root (the package's eager
import chain reads several `metadata/registries/*` files at import time) --
same requirement as `tests/core/test_sdk_revision_gate.py`, whose `planner`
fixture this file reuses verbatim.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

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


# --------------------------------------------------------------------------
# Defect 1: slugs.py's missing `_DRIVER_STATUS_SUFFIX` filter (alp-sdk
# #1169). `nor_flash_driver_status: none` / `emmc_driver_status: none` in a
# V2N-family SoM's `on_module:` block must never be read as a chip slug --
# unfixed, `_slugs_from_on_module` harvested the literal string "none",
# which `kconfig.py::_emit_chips` then wrote as `CONFIG_ALP_SDK_CHIP_NONE=y`
# into alp.conf, a symbol that does not exist in
# zephyr/kconfigs/chips.kconfig -- Zephyr's CMake configure aborts on it
# under `--handwritten-input-configs` (kconf.warn_assign_undef).
# --------------------------------------------------------------------------


def test_a_driver_status_field_is_never_read_as_a_chip_slug(planner):
    """Fails against unfixed `slugs.py`: without the suffix filter, `"none"`
    (and `"planned"`/`"partial"`/`"complete"`, the other declared maturity
    tiers) survive into the returned slug list alongside real chip slugs.

    Verified against unfixed code before the fix landed: this assertion
    failed with `AssertionError: assert 'none' not in ['murata_lbee5hy2fy',
    'none', 'optiga_trust_m']`."""
    from tan.planner import slugs

    on_module = {
        "silicon": "renesas:rzv2n:n44",
        "nor_flash_driver_status": "none",
        "emmc_driver_status": "none",
        "wifi_ble": "murata_lbee5hy2fy",
        "secure_element": "optiga_trust_m",
    }
    result = slugs._slugs_from_on_module(on_module)
    assert "none" not in result
    assert result == ["murata_lbee5hy2fy", "optiga_trust_m"]


def test_the_driver_status_suffix_filter_is_generic_not_enumerated(planner):
    """Matched by suffix, not by an enumerated field name (alp-sdk #1169's
    own rationale for the fix shape): a THIRD, not-yet-invented
    `<x>_driver_status` field must be skipped too, without this file
    needing an update to learn its name."""
    from tan.planner import slugs

    on_module = {"some_future_component_driver_status": "planned"}
    assert slugs._slugs_from_on_module(on_module) == []


# --------------------------------------------------------------------------
# tan-cli#639 (Defect 1's own recurrence -- shipped rather than caught):
# `tan v0.5.1` + `alp-sdk v0.15.0`, the released combination a customer
# actually installs, hard-failed `tan build` on all four Renesas SKUs with
# the exact `CONFIG_ALP_SDK_CHIP_NONE=y` leak the two unit tests above
# guard at the `_slugs_from_on_module` level. Neither test above renders a
# real `alp.conf` fragment, so a regression in the WIRING between
# `slugs.py` and `kconfig.py::_emit_chips`/`_resolve_chip_states` -- as
# opposed to `_slugs_from_on_module` itself -- would pass both of them
# while still shipping the same customer-visible abort. These two tests
# close that gap: they render the real `zephyr-conf` Kconfig fragment (the
# artefact `west build` reads as `alp.conf`) for a real, in-tree
# E1M-V2N101 example against the real bound SoM preset
# (`metadata/e1m_modules/E1M-V2N101.yaml`, which carries
# `nor_flash_driver_status: none` / `emmc_driver_status: none` today), the
# same path `tan generate --target zephyr-conf` / `tan build` take.
# --------------------------------------------------------------------------


def test_zephyr_conf_for_a_real_v2n101_project_does_not_leak_chip_none(planner):
    """tan-cli#639's own repro shape: generate the Kconfig fragment for a
    real V2N101 project and confirm `ALP_SDK_CHIP_NONE` never appears.

    Uses `examples/v2n/v2n-temp-sensor/board.yaml` (`preset: e1m-x-evk`,
    one Zephyr core `m33_sm`) rather than a synthetic board.yaml, so this
    exercises the exact `on_module:` block a customer's own `tan build`
    reads."""
    from tan import planner_emit

    board = SDK / "examples" / "v2n" / "v2n-temp-sensor" / "board.yaml"
    if not board.is_file():
        pytest.skip(f"{board} not present in this alp-sdk checkout")

    text = planner_emit.render(
        "zephyr-conf", sdk_root=SDK, board_yaml=board, core="m33_sm"
    )
    assert "ALP_SDK_CHIP_NONE" not in text, (
        "CONFIG_ALP_SDK_CHIP_NONE=y leaked into the rendered alp.conf "
        "fragment -- exactly what makes `west build` abort with 'attempt "
        "to assign the value \\'y\\' to the undefined symbol "
        "ALP_SDK_CHIP_NONE' (tan-cli#639)"
    )


@pytest.mark.parametrize(
    "board_rel",
    [
        "examples/v2n/v2n-temp-sensor/board.yaml",
        "examples/v2n/v2n-pwm-fan-control/board.yaml",
        "examples/v2n/v2n-rtc-multi-alarm/board.yaml",
    ],
)
def test_no_v2n_zephyr_slice_leaks_chip_none(planner, board_rel):
    """Breadth check across more than one E1M-V2N101 example/core pairing --
    the leak is a property of the SoM preset's `on_module:` block, so it
    fires identically regardless of which project reads it."""
    from tan import planner_emit

    board = SDK / board_rel
    if not board.is_file():
        pytest.skip(f"{board} not present in this alp-sdk checkout")

    project = planner.load_board_yaml(Path(board))
    zephyr_cores = [
        core_id for core_id, core in project.cores.items()
        if core.os == "zephyr"
    ]
    assert zephyr_cores, f"{board_rel} declares no Zephyr core to render"

    for core_id in zephyr_cores:
        text = planner_emit.render(
            "zephyr-conf", sdk_root=SDK, board_yaml=board, core=core_id
        )
        assert "ALP_SDK_CHIP_NONE" not in text, (
            f"{board_rel} core '{core_id}': CONFIG_ALP_SDK_CHIP_NONE=y "
            "leaked into the rendered alp.conf fragment (tan-cli#639)"
        )


# --------------------------------------------------------------------------
# Defect 2: loader.py's missing refusal of `cacheable: true` on a `kind:
# rpmsg` ipc entry (alp-sdk #1088). `<alp/rpc.h>` has no cache-maintenance
# implementation, so honouring the flag silently promises a coherency
# guarantee the code path can't deliver.
# --------------------------------------------------------------------------


def _rpmsg_board_yaml(tmp_path: Path, *, cacheable: bool) -> Path:
    """Same shape as the real `examples/multicore/rpmsg-aen/board.yaml`
    fixture (a32_cluster + m55_hp, `alp_default_rpmsg` over `E1M_I2C0`),
    with `cacheable:` set explicitly so both the accepting and refusing
    case are covered from one board."""
    path = tmp_path / "rpmsg-board.yaml"
    path.write_text(
        "som:\n"
        "  sku: E1M-AEN801\n"
        "  hw_rev: r2\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  a32_cluster:\n"
        "    app: ./linux\n"
        "    image: alp-image-edge\n"
        "  m55_hp:\n"
        "    app: ./m55_hp\n"
        "ipc:\n"
        "  - kind: rpmsg\n"
        "    endpoints: [a32_cluster, m55_hp]\n"
        "    carve_out_kb: 256\n"
        "    name: alp_default_rpmsg\n"
        f"    cacheable: {'true' if cacheable else 'false'}\n",
        encoding="utf-8",
    )
    return path


def test_rpmsg_cacheable_true_refuses(planner, tmp_path):
    """Fails against unfixed `loader.py`: `load_board_yaml` returned a
    `BoardProject` with no exception raised (verified against unfixed code:
    this `pytest.raises` block did not enter, failing with `Failed: DID NOT
    RAISE`)."""
    board = _rpmsg_board_yaml(tmp_path, cacheable=True)
    with pytest.raises(planner.OrchestratorError) as excinfo:
        planner.load_board_yaml(board)

    message = str(excinfo.value)
    assert "alp_default_rpmsg" in message   # which entry
    assert "rpmsg" in message
    assert "cacheable: true" in message
    assert "#1088" in message               # names the tracking issue, not just refuses


def test_rpmsg_cacheable_false_or_unset_still_loads(planner, tmp_path):
    """The success path is unchanged: the new refusal must not reject the
    real `rpmsg-aen` example's own shape (no `cacheable:` key at all is the
    example's actual form; explicit `false` is the equivalent covered here
    since this fixture always emits the key)."""
    board = _rpmsg_board_yaml(tmp_path, cacheable=False)
    project = planner.load_board_yaml(board)
    assert project.sku == "E1M-AEN801"


# --------------------------------------------------------------------------
# Defect 3: loader.py's `_load_yaml`/`_load_json` accepting duplicate
# mapping keys (alp-sdk #1127). `yaml.safe_load` silently keeps only the
# LAST value of a repeated key, so a board.yaml that repeats `sku:` under
# `som:` silently retargets the build to a different module instead of
# refusing -- `tan validate` (which shells the SDK's duplicate-key-rejecting
# validator) already refuses this file; unfixed, `tan build`'s own loader
# did not.
# --------------------------------------------------------------------------


def test_a_duplicate_sku_key_refuses_instead_of_silently_retargeting(planner, tmp_path):
    """Fails against unfixed `loader.py`: unfixed, `yaml.safe_load` resolved
    this to `sku: E1M-AEN301` (last-key-wins) with no error, so
    `load_board_yaml` returned a `BoardProject` for the WRONG module instead
    of raising -- verified against unfixed code: this `pytest.raises` block
    did not enter, and `project.sku` (had the call not raised) read
    `'E1M-AEN301'`, not the first-written `'E1M-AEN801'`."""
    path = tmp_path / "duplicate-key-board.yaml"
    path.write_text(
        "som:\n"
        "  sku: E1M-AEN801\n"
        "  sku: E1M-AEN301\n"
        "  hw_rev: r2\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  m55_hp:\n"
        "    app: ./src\n",
        encoding="utf-8",
    )
    with pytest.raises(planner.OrchestratorError) as excinfo:
        planner.load_board_yaml(path)

    message = str(excinfo.value)
    assert "duplicate key" in message
    assert "sku" in message


def test_a_clean_board_yaml_with_no_duplicates_still_loads(planner, tmp_path):
    """The success path is unchanged: a board.yaml with no repeated key
    must not trip the new duplicate-key check."""
    path = tmp_path / "clean-board.yaml"
    path.write_text(
        "som:\n"
        "  sku: E1M-AEN801\n"
        "  hw_rev: r2\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  m55_hp:\n"
        "    app: ./src\n",
        encoding="utf-8",
    )
    project = planner.load_board_yaml(path)
    assert project.sku == "E1M-AEN801"
