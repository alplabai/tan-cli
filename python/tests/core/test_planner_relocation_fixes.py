# SPDX-License-Identifier: Apache-2.0
"""tan-cli#485 (`#320` recurred): three of the four defects that issue names,
each with a dedicated regression test that fails against the unfixed code.

Defect 4 (`project_loader.py`'s missing `SdkRevisionNotBuildable` gate) has
its own test in `tests/core/test_project_loader.py`, alongside the sibling
`SdkRevisionUnknown` coverage it belongs next to.

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
