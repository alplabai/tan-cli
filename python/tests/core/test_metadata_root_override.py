# SPDX-License-Identifier: Apache-2.0
"""`load_board_yaml(..., metadata_root=...)` threads through storage/partition
/carveout/capability resolution (tan-cli#573) -- NOT every reader downstream
of a loaded project. See "Still bound-root-only" below for what this module
does NOT cover.

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

Still bound-root-only, and NOT exercised by this module: any reader that
takes the project (or a name) but not the root and reads `paths.METADATA_ROOT`
or `paths.REPO` itself. `tan.planner.kconfig._chip_has_driver` and
`_emit_extra_library_profile` are direct examples (`kconfig.py:706`, `:91`
read the module-level `REPO` even with a project in hand). The library-manifest
layer (`libraries.py`, imported into `kconfig.py` as `_library_layer`) is a
mixed case, not a clean "no project in hand" one: `resolve_selection` and
`zephyr_kconfig_lines` default `metadata_root` to the bound `METADATA_ROOT`
when their caller doesn't supply one; `_check_requires` has no default at
all (`metadata_root: Path` is a required positional, libraries.py:285-290)
and just gets whatever `resolve_selection` was given. `kconfig.py` calls into
`_library_layer` at six sites (:928, :947, :1873, :1931, :1935, :2012 -- five
of the six take a `metadata_root` argument, `_emit_library_hw_backends` does
not), and none of the six pass `project.effective_metadata_root()` through,
so they still resolve against the bound tree. That is a separate module from
`som_metadata.resolve_capabilities`, whose three call sites inside
`kconfig.py` ARE threaded -- part of what this change fixes (see above). A
handful of other constants are frozen at import against the bound root
regardless of any project (`slugs.py`'s `PERIPHERAL_KCONFIG_REGISTRY`,
`som_metadata.py`'s `SILICON_KCONFIG_REGISTRY`, `validate.py`'s
`_CURATED_LIBRARIES`). None of that is covered here.

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

# The SoM under test and the SRAM bank renamed in the ALTERNATE tree.  E3 is
# the right silicon for this: `metadata/e1m_modules/E1M-AEN301.yaml` declares
# NO `memory_map:` block, so `resolve_memory_map` takes the SoC-JSON-derived
# branch -- the one that reads the metadata root -- rather than returning the
# preset's override verbatim.
_SKU = "E1M-AEN301"
_SOC_JSON = ("socs", "alif", "ensemble", "e3.json")
_BANK = "SRAM6"
_RENAMED = "SRAM6X"
_DEVICE = _RENAMED.lower()          # region names are lower-cased by the resolver


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
    for variant in doc["variants"]:
        banks = variant.get("sram_banks_kb") or {}
        if _BANK in banks:
            variant["sram_banks_kb"] = {
                (_RENAMED if k == _BANK else k): v for k, v in banks.items()}
            renamed += 1
    assert renamed, (
        f"no variant in {'/'.join(_SOC_JSON)} declares an {_BANK} bank -- the "
        f"fixture's premise moved; pick another bank")
    soc.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return dest


def _board(tmp_path: Path, tail: str) -> Path:
    path = tmp_path / "board.yaml"
    path.write_text(
        "som:\n"
        f"  sku: {_SKU}\n"
        "preset: e1m-evk\n"
        "cores:\n"
        "  m55_hp:\n"
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
    memory_map region nor an on_module.ospi_memories key on SoM E1M-AEN301"."""
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
    assert [(p.name, p.status, p.reason) for p in parts] == [("userdata", "ok", None)]


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
