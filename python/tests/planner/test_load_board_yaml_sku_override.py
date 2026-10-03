# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2597 (alp-sdk `7bde5112d`): `load_board_yaml(..., sku=...)`
resolves the SoM facts for the SKU being BUILT rather than the single static
`som.sku` the board.yaml declares, so an example that builds for sibling SoM
SKUs gets a per-SKU alp.conf. alp-sdk exercises it through
`scripts/gen_example_alp_conf.py` (not mirrored in tan); this pins the
loader half the mirror carries.

Real-SDK-gated: reads the real E1M-AEN801 / E1M-AEN401 presets.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- the override is resolved against real SoM presets.",
)


def _write_board(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "board.yaml"
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return path


_AEN801_BOARD = """
name: test-sku-override
som:
  sku: E1M-AEN801
cores:
  m55_hp:
    os: zephyr
    app: ./m55_hp
"""


def test_no_override_keeps_the_declared_sku(tmp_path: Path) -> None:
    from tan.planner import load_board_yaml

    project = load_board_yaml(_write_board(tmp_path, _AEN801_BOARD))
    assert project.sku == "E1M-AEN801"
    assert project.som_preset.get("sku") == "E1M-AEN801"


def test_override_resolves_the_sibling_skus_preset(tmp_path: Path) -> None:
    from tan.planner import load_board_yaml

    project = load_board_yaml(
        _write_board(tmp_path, _AEN801_BOARD), sku="E1M-AEN401")
    assert project.sku == "E1M-AEN401"
    assert project.som_preset.get("sku") == "E1M-AEN401"


def test_override_drives_sku_derived_refusals(tmp_path: Path) -> None:
    """The override is applied BEFORE every SKU-derived check, so a
    security block valid on the declared SKU is judged against the
    overriding one (here: OPTIGA is fitted on AEN401, DNP on AEN801)."""
    from tan.planner import load_board_yaml
    from tan.planner.models import OrchestratorError

    path = _write_board(tmp_path, """
        name: test-sku-override-optiga
        som:
          sku: E1M-AEN401
        cores:
          m55_hp:
            os: zephyr
            app: ./m55_hp
        security:
          psa:
            attestation_root: optiga_trust_m
            tfm: true
    """)
    load_board_yaml(path)
    with pytest.raises(OrchestratorError, match="not assembled on SoM E1M-AEN801"):
        load_board_yaml(path, sku="E1M-AEN801")
