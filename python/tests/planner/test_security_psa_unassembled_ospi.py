# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2311: `security.psa.its_storage` / `ps_storage` must not resolve
to an `on_module.ospi_memories:` key the SoM preset declares `assembled:
false` -- ported line-for-line alongside alp-sdk's
`tests/scripts/test_orchestrate_security.py` additions for the same bug.
On `E1M-AEN801`, whose preset marks `ospi0`/`ospi1` unfitted,
`its_storage: ospi0` used to be accepted by the loader's cross-field check
because it only checked `on_module.ospi_memories:` KEY membership, not
`assembled`.

Real-SDK-gated (same requirement as `test_storage_unassembled_flash.py`):
needs the real E1M-AEN801/E1M-AEN803/E1M-AEN301 SoM presets from a bound
alp-sdk checkout.

Run locally:

    python -m pytest python/tests/planner/test_security_psa_unassembled_ospi.py -v
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


def _its_ospi0_board(sku: str) -> str:
    return f"""
    name: test-{sku.lower()}-its-ospi0
    som:
      sku: {sku}
    cores:
      m55_hp:
        os: zephyr
        app: ./m55_hp
    security:
      psa:
        its_storage: ospi0
        tfm: true
    """


def test_security_psa_its_storage_refused_on_unassembled_ospi(
    tmp_path: Path,
) -> None:
    """E1M-AEN801 declares `ospi0` `assembled: false` -- naming it as
    `security.psa.its_storage:` must be refused with the specific "not
    assembled" reason, not the generic "does not resolve" message."""
    from tan.planner import load_board_yaml
    from tan.planner.models import OrchestratorError

    path = _write_board(tmp_path, _its_ospi0_board("E1M-AEN801"))
    with pytest.raises(OrchestratorError) as excinfo:
        load_board_yaml(path)
    msg = str(excinfo.value)
    assert "security.psa.its_storage" in msg
    assert "ospi0" in msg
    assert "E1M-AEN801" in msg
    assert "assembled: false" in msg
    assert "metadata/e1m_modules/E1M-AEN801.yaml" in msg


def _ps_ospi0_board(sku: str) -> str:
    return f"""
    name: test-{sku.lower()}-ps-ospi0
    som:
      sku: {sku}
    cores:
      m55_hp:
        os: zephyr
        app: ./m55_hp
    security:
      psa:
        its_storage: mram_main
        ps_storage: ospi0
        tfm: true
    """


def test_security_psa_ps_storage_refused_on_unassembled_ospi(
    tmp_path: Path,
) -> None:
    """#2311: same guard, `ps_storage` side -- E1M-AEN801 declares
    `ospi0` `assembled: false`, so naming it as `security.psa.ps_storage:`
    must be refused with the specific "not assembled" reason."""
    from tan.planner import load_board_yaml
    from tan.planner.models import OrchestratorError

    path = _write_board(tmp_path, _ps_ospi0_board("E1M-AEN801"))
    with pytest.raises(OrchestratorError) as excinfo:
        load_board_yaml(path)
    msg = str(excinfo.value)
    assert "security.psa.ps_storage" in msg
    assert "ospi0" in msg
    assert "E1M-AEN801" in msg
    assert "assembled: false" in msg
    assert "metadata/e1m_modules/E1M-AEN801.yaml" in msg


def test_security_psa_its_storage_accepted_on_assembled_ospi(
    tmp_path: Path,
) -> None:
    """E1M-AEN803 fits `ospi0` (`assembled: true`) -- unaffected."""
    from tan.planner import emit_tfm_sysbuild_conf, load_board_yaml

    path = _write_board(tmp_path, _its_ospi0_board("E1M-AEN803"))
    project = load_board_yaml(path)
    out = emit_tfm_sysbuild_conf(project)
    assert 'CONFIG_PSA_CRYPTO_ITS_BACKING_STORE="ospi0"' in out


def test_security_psa_its_storage_accepted_on_optional_ospi(
    tmp_path: Path,
) -> None:
    """E1M-AEN301 declares `ospi0` `assembled: optional` -- `optional`
    is not the literal `False` the guard keys on, so it stays legal."""
    from tan.planner import emit_tfm_sysbuild_conf, load_board_yaml

    path = _write_board(tmp_path, _its_ospi0_board("E1M-AEN301"))
    project = load_board_yaml(path)
    out = emit_tfm_sysbuild_conf(project)
    assert 'CONFIG_PSA_CRYPTO_ITS_BACKING_STORE="ospi0"' in out
