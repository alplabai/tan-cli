# SPDX-License-Identifier: Apache-2.0
"""`load_board_yaml(..., metadata_root=...)` reads ONE tree (tan-cli#573).

The override is documented and used (`tests/core/test_sdk_revision_gate.py`,
`tan/core/doctor_libraries.py::_resolve`), but two of `load_board_yaml`'s five
stages ignored it and read the module-level bound `paths.METADATA_ROOT`
instead:

    loader.py  `_resolve_storage`        -> `_known_flash_devices(..., METADATA_ROOT)`
    loader.py  `_validate_cross_fields`  -> `resolve_memory_map(..., METADATA_ROOT)`

The mismatch was PARTIAL, which is what made it hard to see: `som_preset`
itself still came from the caller's tree, so an alternate tree's explicit
`memory_map:` override and its `on_module.ospi_memories:` keys WERE honoured
-- only the SoC-JSON-derived branch of `resolve_memory_map` read the wrong
tree.  So a `storage[].flash_device:` naming an SRAM bank the alternate tree's
SoC JSON declares was refused, and the message blamed the customer's
board.yaml while listing the OTHER tree's device names.

Fixing only those two lines is not enough, and the third test below is why:
the resolvers the loader hands the project to (`resolve_storage_partitions`,
`resolve_carve_outs`, the Kconfig capability emitters) read the bound root
too, so a loader-only fix makes the loader ACCEPT a device the resolver then
BLOCKS.  The root therefore travels on `BoardProject.metadata_root`, and
those resolvers read it back via `effective_metadata_root()`.

Every test here needs a real metadata tree AND a bound root -- same
requirement, and the same env vars, as `tests/core/test_sdk_revision_gate.py`
and `tests/parity/test_planner_emit_parity.py`.
"""

from __future__ import annotations

import json
import os
import shutil
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

# The SoM under test and the SoC memory region renamed in the ALTERNATE tree.
#
# This MUST be a SoM whose preset declares NO `memory_map:` block, so
# `resolve_memory_map` takes the SoC-JSON-derived branch -- the one that reads
# the metadata root, and the one the bug in this module's docstring lived in.
# A SoM with an explicit override returns it verbatim and never consults the
# root at all, so the rename below would be invisible and every assertion here
# would pass for the wrong reason.
#
# Was E1M-AEN301 / e3.json / `SRAM6` until alp-sdk#1447 gave all six AEN SoMs
# an explicit `memory_map:` (disjoint he_slot0/hp_slot0 windows, alp-sdk#1295
# / #1445), which moved the whole Alif family onto the override branch and
# silently emptied this fixture -- `_known_flash_devices("E1M-AEN301")` now
# answers from the preset, not the SoC JSON. Measured on alp-sdk
# `bd8be484680cf5aa1c1ac0e8b38d84128b5a279d`:
#
#   E1M-AEN301 -> ['atoc','he_slot0','hp_slot0','mcuboot','mram_main',
#                  'ospi0','ospi1','reserved','storage']   (override branch)
#   E1M-V2N101 -> ['ddr_main', 'm33_tcm', 'ocram_low']     (derived branch)
#   E1M-V2M101 -> ['ddr_main', 'm33_tcm', 'ocram_low']     (derived branch)
#
# V2N/V2M are also the right home for this test on their own merits: they are
# the only SKUs published with `preliminary: false` AND
# `partial_hw_config: false`, i.e. the parts a customer can actually build
# against today. A regression test for a customer-facing misdiagnosis belongs
# on the silicon customers are using, not on a preliminary part -- and as more
# SoMs gain explicit `memory_map:` overrides, the derived branch converges on
# exactly this family, so this pin is the durable one.
_SKU = "E1M-V2N101"
_SOC_JSON = ("socs", "renesas", "rzv2n", "n44.json")
_BANK = "ocram_low"
_RENAMED = "ocram_lowx"
_DEVICE = _RENAMED.lower()          # region names are lower-cased by the resolver
#: Board preset hosting this SoM family, and the Zephyr core to hang the
#: app off -- both family-specific, so they move with `_SKU`.
_BOARD = "e1m-x-evk"          # preset ids are the lower-case file stem
_CORE = "m33_sm"


@pytest.fixture(scope="module")
def planner():
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root
    bind_sdk_root(SDK)
    import tan.planner as planner_pkg
    return planner_pkg


@pytest.fixture(scope="module")
def alternate_tree(tmp_path_factory) -> Path:
    """A full metadata copy whose E3 SoC JSON renames `SRAM6` -> `SRAM6X`.

    A COPY of the whole tree, not a synthetic stub: the point is that the two
    roots are identical except for the one fact under test, so a difference in
    the loader's answer can only have come from reading the wrong root.
    """
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    dest = tmp_path_factory.mktemp("alt") / "metadata"
    shutil.copytree(SDK / "metadata", dest)
    soc = dest.joinpath(*_SOC_JSON)
    doc = json.loads(soc.read_text(encoding="utf-8"))
    renamed = 0
    # `memory_regions[]` entries, not `variants[].sram_banks_kb` -- the RZ/V2N
    # SoC JSON declares its regions at the top level and its variants carry no
    # bank map at all, so the pre-alp-sdk#1447 Alif-shaped walk found nothing
    # here and the fixture would have silently renamed zero entries.
    for region in doc.get("memory_regions") or []:
        if region.get("name") == _BANK:
            region["name"] = _RENAMED
            renamed += 1
    assert renamed, (
        f"no memory_regions[] entry in {'/'.join(_SOC_JSON)} is named "
        f"{_BANK} -- the "
        f"fixture's premise moved; pick another bank")
    soc.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return dest


def _board(tmp_path: Path, tail: str) -> Path:
    path = tmp_path / "board.yaml"
    path.write_text(
        "som:\n"
        f"  sku: {_SKU}\n"
        # E1M-X-EVK, not e1m-evk: the latter hosts only the alif-ensemble and
        # nxp-imx9 families and refuses a renesas-rzv2n SoM outright.
        f"preset: {_BOARD}\n"
        "cores:\n"
        f"  {_CORE}:\n"
        "    app: ./src\n" + tail,
        encoding="utf-8")
    return path


def test_the_two_trees_differ_in_exactly_the_bank_under_test(planner, alternate_tree):
    """Guards the fixture itself: if the bound tree already knew `sram6x`,
    every assertion below would pass for the wrong reason."""
    from tan.planner.partition import _known_flash_devices
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.project_loader import _resolve_sku

    bound = set(_known_flash_devices(_resolve_sku(_SKU, METADATA_ROOT), METADATA_ROOT))
    alt = set(_known_flash_devices(_resolve_sku(_SKU, alternate_tree), alternate_tree))
    assert _DEVICE in alt and _DEVICE not in bound
    assert alt - bound == {_DEVICE}
    assert bound - alt == {_BANK.lower()}


def test_storage_flash_device_resolves_against_the_requested_tree(
        planner, alternate_tree, tmp_path):
    """Stage 4.  Before the fix this raised, and the message listed the BOUND
    tree's device names while blaming the customer's board.yaml."""
    board = _board(tmp_path,
                   "storage:\n"
                   "  - name: userdata\n"
                   "    size_kib: 64\n"
                   f"    flash_device: {_DEVICE}\n")
    project = planner.load_board_yaml(board, metadata_root=alternate_tree)
    assert [e.flash_device for e in project.storage] == [_DEVICE]


def test_psa_backing_store_resolves_against_the_requested_tree(
        planner, alternate_tree, tmp_path):
    """Stage 5.  No `storage:` block at all, so the reference can only resolve
    through `resolve_memory_map` -- the call that read the wrong root."""
    board = _board(tmp_path,
                   "security:\n"
                   "  psa:\n"
                   f"    its_storage: {_DEVICE}\n")
    project = planner.load_board_yaml(board, metadata_root=alternate_tree)
    assert project.security["psa"]["its_storage"] == _DEVICE


def test_the_partition_resolver_reads_the_same_tree_the_loader_did(
        planner, alternate_tree, tmp_path):
    """The half a two-line loader fix would miss: `resolve_storage_partitions`
    read the bound root, so it blocked the very entry the loader had just
    accepted -- `status='blocked'`, reason "flash device 'sram6x' is neither a
    memory_map region nor an on_module.ospi_memories key on SoM E1M-AEN301".

    What is asserted is WHICH TREE the resolver looked in, not the verdict.
    That distinction became load-bearing in tan-cli#868: alp-sdk#1556 now
    refuses `status: ok` for any `memory_map:` device whose Devicetree label
    merely DEFAULTED to the device name, and the alternate tree's `sram6x`
    declares no explicit `dt_label:`, so this entry blocks on that gate. The
    reason still names `sram6x` as a device the resolver RESOLVED -- which is
    the proof this test exists for -- where reading the bound root would
    still produce the "neither a memory_map region nor an
    on_module.ospi_memories key" message instead.
    """
    from tan.planner.partition import resolve_storage_partitions

    board = _board(tmp_path,
                   "storage:\n"
                   "  - name: userdata\n"
                   "    size_kib: 64\n"
                   f"    flash_device: {_DEVICE}\n")
    project = planner.load_board_yaml(board, metadata_root=alternate_tree)
    assert project.metadata_root == alternate_tree
    assert project.effective_metadata_root() == alternate_tree

    parts = resolve_storage_partitions(project)
    assert [p.name for p in parts] == ["userdata"]
    reason = parts[0].reason or ""
    assert "neither a memory_map region" not in reason, reason
    assert f"flash device '{_DEVICE}'" in reason or _DEVICE in reason, reason
    assert "Devicetree label defaults to" in reason, reason


def test_a_default_load_still_records_and_reads_the_bound_root(planner, tmp_path):
    """The override is the exception; the default must be unchanged.  A
    project loaded with no `metadata_root=` carries the bound root, and a
    hand-constructed `BoardProject` (no loader involved) still falls back to
    it."""
    from tan.planner.paths import METADATA_ROOT

    board = _board(tmp_path, "")
    project = planner.load_board_yaml(board)
    assert project.metadata_root == METADATA_ROOT
    assert project.effective_metadata_root() == METADATA_ROOT

    hand_built = planner.BoardProject(
        sku=_SKU, hw_rev=None, board_name=None, board_hw_rev=None,
        cores={}, ipc=[], soc_spec={}, som_preset={}, board_preset=None)
    assert hand_built.metadata_root is None
    assert hand_built.effective_metadata_root() == METADATA_ROOT
