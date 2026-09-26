# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2311: `storage[].flash_device:` must not resolve to a part the
SoM preset declares `assembled: false` -- ported line-for-line alongside
alp-sdk's `tests/scripts/test_orchestrate_storage_unassembled_flash.py`.
On `E1M-AEN801`, whose preset marks `ospi0`/`ospi1`/`hyperram` unfitted,
`_resolve_flash_device("ospi0", ...)` used to return a live 32 MiB
descriptor (from `capacity_mbit` alone) and `_known_flash_devices()`
still advertised `ospi0`/`ospi1` to the loader's cross-check.

Real-SDK-gated (same requirement as `test_storage_write_authority.py`):
needs the real E1M-AEN801/E1M-AEN803/E1M-AEN301 SoM presets from a bound
alp-sdk checkout.

Run locally:

    python -m pytest python/tests/planner/test_storage_unassembled_flash.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.partition requires SOME "
           "bound root (tan/planner_root.py), and every case here reads a "
           "real SoM preset from the bound checkout.",
)


def _write_board(tmp_path: Path, body: str, name: str = "board.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return path


def _board(sku: str, flash_device: str) -> str:
    return f"""
    name: test-2311-{sku.lower()}-{flash_device}
    som:
      sku: {sku}
    cores:
      m55_hp: {{ os: zephyr, app: ./m55_hp }}
    storage:
      - {{ name: app_data, size_kib: 64, fs: littlefs, flash_device: {flash_device} }}
    """


def test_aen801_ospi0_refused_by_loader(tmp_path: Path) -> None:
    """E1M-AEN801 declares `ospi0` `assembled: false` -- it must not be
    a known device the loader's cross-check accepts."""
    from tan.planner import load_board_yaml
    from tan.planner.models import OrchestratorError

    path = _write_board(tmp_path, _board("E1M-AEN801", "ospi0"))
    with pytest.raises(OrchestratorError, match="ospi0"):
        load_board_yaml(path)


def test_aen801_ospi0_not_in_known_flash_devices() -> None:
    from tan.planner.partition import _known_flash_devices
    from tan.planner.paths import METADATA_ROOT

    som_preset = {
        "sku": "E1M-AEN801",
        "on_module": {"ospi_memories": {
            "ospi0": {"assembled": False, "capacity_mbit": 256},
            "ospi1": {"assembled": False, "capacity_mbit": "TBD"},
        }},
    }
    known = _known_flash_devices(som_preset, METADATA_ROOT)
    assert "ospi0" not in known
    assert "ospi1" not in known


def test_aen801_ospi0_refused_by_resolver_directly() -> None:
    """Defense in depth: a hand-built project calling the resolver
    directly (skipping `_known_flash_devices()`) must still be
    refused, with the SKU, the device, and the preset file named."""
    from tan.planner.partition import _resolve_flash_device
    from tan.planner.paths import METADATA_ROOT

    som_preset = {
        "sku": "E1M-AEN801",
        "on_module": {"ospi_memories": {
            "ospi0": {"assembled": False, "capacity_mbit": 256},
        }},
    }
    descriptor, reason = _resolve_flash_device(
        "ospi0", som_preset, METADATA_ROOT)
    assert descriptor is None, descriptor
    assert "ospi0" in reason
    assert "E1M-AEN801" in reason
    assert "assembled: false" in reason
    assert "metadata/e1m_modules/E1M-AEN801.yaml" in reason


def test_aen803_ospi0_accepted(tmp_path: Path) -> None:
    """E1M-AEN803 fits `ospi0` (`assembled: true`) -- unaffected."""
    from tan.planner import load_board_yaml
    from tan.planner.partition import _known_flash_devices, _resolve_flash_device
    from tan.planner.paths import METADATA_ROOT

    path = _write_board(tmp_path, _board("E1M-AEN803", "ospi0"))
    project = load_board_yaml(path)
    known = _known_flash_devices(project.som_preset, METADATA_ROOT)
    assert "ospi0" in known
    descriptor, reason = _resolve_flash_device(
        "ospi0", project.som_preset, METADATA_ROOT)
    assert reason is None, reason
    assert descriptor is not None
    assert descriptor["name"] == "ospi0"


def test_aen301_ospi0_optional_accepted(tmp_path: Path) -> None:
    """E1M-AEN301 declares `ospi0` `assembled: optional` -- `optional`
    is not the literal `False` the guard keys on, so it stays legal."""
    from tan.planner import load_board_yaml
    from tan.planner.partition import _known_flash_devices, _resolve_flash_device
    from tan.planner.paths import METADATA_ROOT

    path = _write_board(tmp_path, _board("E1M-AEN301", "ospi0"))
    project = load_board_yaml(path)
    known = _known_flash_devices(project.som_preset, METADATA_ROOT)
    assert "ospi0" in known
    descriptor, reason = _resolve_flash_device(
        "ospi0", project.som_preset, METADATA_ROOT)
    assert reason is None, reason
    assert descriptor is not None


def test_mram_main_unaffected_on_aen801(tmp_path: Path) -> None:
    """`mram_main` is a `memory_map:` region, not an
    `on_module.ospi_memories:` entry -- the new guard only inspects the
    latter, so AEN801's on-die MRAM path must resolve exactly as
    before."""
    from tan.planner import load_board_yaml
    from tan.planner.partition import _known_flash_devices, _resolve_flash_device
    from tan.planner.paths import METADATA_ROOT

    path = _write_board(tmp_path, _board("E1M-AEN801", "mram_main"))
    project = load_board_yaml(path)
    known = _known_flash_devices(project.som_preset, METADATA_ROOT)
    assert "mram_main" in known
    descriptor, reason = _resolve_flash_device(
        "mram_main", project.som_preset, METADATA_ROOT)
    assert reason is None, reason
    assert descriptor is not None
    assert descriptor["name"] == "mram_main"
